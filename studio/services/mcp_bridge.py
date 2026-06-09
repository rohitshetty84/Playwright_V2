"""
Playwright MCP Bridge — JSON-RPC 2.0 client for the @playwright/mcp server.

Auth strategy (when storage_state is provided):
    Python Playwright launches the browser directly with new_context(storage_state=...)
    — the same mechanism the old Playwright CLI used, which reliably loads SAP SSO
    session cookies. @playwright/mcp then connects to that running browser via CDP
    and inherits its authenticated context.

    Without storage_state: @playwright/mcp launches and owns the browser itself.

Usage:
    bridge = PlaywrightMCPBridge(storage_state="studio/.auth/sf.json", headless=True)
    await bridge.start()
    try:
        result = await bridge.call_tool("browser_navigate", {"url": "https://..."})
        snapshot = await bridge.call_tool("browser_snapshot", {})
    finally:
        await bridge.stop()
"""

import asyncio
import json
import logging
import random
from pathlib import Path
from typing import Optional

logger = logging.getLogger("playwright_ai_studio")


class PlaywrightMCPBridge:
    """Async wrapper around the @playwright/mcp subprocess.

    Speaks JSON-RPC 2.0 over the subprocess stdin/stdout (newline-delimited).
    Exposes:
        azure_tool_definitions  — list[dict] ready to pass to Azure OpenAI tools=
        call_tool(name, args)   — proxy a tool call to the MCP server
        get_current_url()       — helper that evaluates window.location.href
    """

    _PROTOCOL_VERSION = "2024-11-05"

    def __init__(
        self,
        storage_state: Optional[str] = None,
        headless: bool = True,
        browser: str = "chromium",
    ) -> None:
        self._storage_state = storage_state
        self._headless      = headless
        self._browser       = browser

        self._proc: Optional[asyncio.subprocess.Process] = None
        self._req_id = 0
        # Serialize all JSON-RPC requests — the MCP stdio transport is
        # sequential: one in-flight request at a time.
        self._lock = asyncio.Lock()

        # Auth-mode objects (Python Playwright owns the browser)
        self._pw_instance  = None
        self._auth_browser = None
        self._auth_ctx     = None

        # Populated by start()
        self.tools: list[dict] = []
        self.azure_tool_definitions: list[dict] = []

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Launch the MCP server and complete the protocol handshake.

        Auth mode (storage_state provided):
            Python Playwright launches the browser with new_context(storage_state=...)
            — reliable SAP SSO cookie loading, identical to the old CLI approach.
            @playwright/mcp then connects to that browser via CDP and inherits the
            authenticated context.

        No-auth mode:
            @playwright/mcp launches and owns the browser directly.
        """
        # Remove stale @playwright/mcp browser-profile lock dirs before launch.
        # When a container is killed mid-exploration these dirs persist and the
        # next run fails with "Browser is already in use". We also pass
        # --isolated, but proactive cleanup is belt-and-suspenders.
        _mcp_locks = Path("/ms-playwright")
        if _mcp_locks.exists():
            import glob, shutil as _shutil
            for _ld in glob.glob(str(_mcp_locks / "mcp-*")):
                try:
                    _shutil.rmtree(_ld, ignore_errors=True)
                except Exception:
                    pass

        if self._storage_state and Path(self._storage_state).exists():
            cmd = await self._start_with_auth()
        else:
            cmd = self._start_without_auth()

        logger.info(f"[MCP] Starting: {' '.join(cmd)}")
        self._proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=20 * 1024 * 1024,  # 20 MB — MCP screenshot responses can be 1-2 MB base64
        )

        # MCP handshake: initialize → (notification) initialized → tools/list
        await self._request("initialize", {
            "protocolVersion": self._PROTOCOL_VERSION,
            "capabilities":    {},
            "clientInfo":      {"name": "playwright-ai-studio", "version": "1.0.0"},
        })
        # Notification — no response expected
        await self._send_raw({
            "jsonrpc": "2.0",
            "method":  "notifications/initialized",
            "params":  {},
        })

        result = await self._request("tools/list", {})
        self.tools                  = result.get("tools", [])
        self.azure_tool_definitions = [self._to_azure_tool(t) for t in self.tools]
        logger.info(f"[MCP] Ready — {len(self.tools)} tool(s): "
                    f"{', '.join(t['name'] for t in self.tools)}")

    async def _start_with_auth(self) -> list:
        """Python Playwright launches Chrome with the storage state loaded, then
        @playwright/mcp connects to it via CDP — cookies are guaranteed applied."""
        from playwright.async_api import async_playwright

        cdp_port = random.randint(19100, 19900)

        self._pw_instance = await async_playwright().start()

        launch_opts: dict = {
            "headless": self._headless,
            "args":     [f"--remote-debugging-port={cdp_port}"],
        }
        # "chrome" channel requires Google Chrome installed; fall back to
        # the open-source Chromium build that Playwright always ships.
        if self._browser in ("chrome", "msedge"):
            try:
                self._auth_browser = await self._pw_instance.chromium.launch(
                    channel=self._browser, **launch_opts
                )
            except Exception:
                logger.warning(
                    f"[MCP] {self._browser} channel not found — falling back to Chromium"
                )
                self._auth_browser = await self._pw_instance.chromium.launch(**launch_opts)
        else:
            self._auth_browser = await self._pw_instance.chromium.launch(**launch_opts)

        # Apply the storage state — same call that worked in the old Playwright CLI
        self._auth_ctx = await self._auth_browser.new_context(
            storage_state=self._storage_state
        )
        # Open a blank page so the context is live when MCP connects
        await self._auth_ctx.new_page()

        cdp_url = f"http://localhost:{cdp_port}"
        logger.info(
            f"[MCP] Auth-mode: Python-Playwright browser on CDP {cdp_url} "
            f"(storage_state={Path(self._storage_state).name})"
        )
        return ["npx", "--yes", "@playwright/mcp",
                "--cdp-endpoint", cdp_url,
                "--caps", "vision"]

    def _start_without_auth(self) -> list:
        """@playwright/mcp owns the browser (no session needed)."""
        # @playwright/mcp only accepts "chromium", "firefox", "webkit" as --browser values.
        # Branded channels (chrome, msedge) must be expressed as "chromium" here;
        # the channel selection happens at the Python-Playwright level in _start_with_auth.
        browser_arg = "chromium" if self._browser in ("chrome", "msedge") else self._browser
        cmd = ["npx", "--yes", "@playwright/mcp", "--browser", browser_arg,
               "--isolated",           # fresh profile per session — no stale lock files
               "--caps", "vision"]
        if self._headless:
            cmd.append("--headless")
        return cmd

    async def stop(self) -> None:
        """Terminate the MCP server and, in auth mode, the Python-Playwright browser."""
        if self._proc and self._proc.returncode is None:
            try:
                self._proc.terminate()
                await asyncio.wait_for(self._proc.wait(), timeout=5.0)
            except Exception:
                pass

        # Auth-mode cleanup — Python Playwright owns these
        for obj, method in [
            (self._auth_ctx,     "close"),
            (self._auth_browser, "close"),
            (self._pw_instance,  "stop"),
        ]:
            if obj is not None:
                try:
                    await getattr(obj, method)()
                except Exception:
                    pass

        logger.info("[MCP] Server stopped")

    # ── Tool call ─────────────────────────────────────────────────────────────

    async def call_tool(self, name: str, arguments: dict) -> dict:
        """Call an MCP tool by name and return a normalised result dict.

        Returns:
            {
                "text":           str,        # human-readable text content
                "screenshot_b64": str | None, # base64 PNG for browser_screenshot
                "content":        list,       # raw MCP content array
            }
        """
        raw = await self._request("tools/call", {"name": name, "arguments": arguments})
        return self._parse_result(raw)

    async def get_current_url(self) -> str:
        """Return the current page URL via browser_evaluate."""
        try:
            r = await self.call_tool("browser_evaluate", {"function": "() => window.location.href"})
            return r.get("text", "").strip().strip('"').strip("'")
        except Exception:
            return ""

    # ── JSON-RPC transport ────────────────────────────────────────────────────

    async def _request(self, method: str, params: dict) -> dict:
        """Send a JSON-RPC request; return the result. Serialised via lock."""
        async with self._lock:
            self._req_id += 1
            req_id = self._req_id

            await self._send_raw({
                "jsonrpc": "2.0",
                "id":      req_id,
                "method":  method,
                "params":  params,
            })

            # Read lines until we get the response matching our req_id.
            # The server may emit notification lines (no "id") between responses.
            while True:
                line = await asyncio.wait_for(
                    self._proc.stdout.readline(),
                    timeout=90.0,
                )
                if not line:
                    raise RuntimeError(f"[MCP] Server closed stdout during {method}")

                line = line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue

                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    # Skip non-JSON lines (startup banners, etc.)
                    continue

                # Skip notifications (no "id") and unrelated responses
                if msg.get("id") != req_id:
                    continue

                if "error" in msg:
                    err = msg["error"]
                    raise RuntimeError(
                        f"[MCP] {method} error {err.get('code')}: {err.get('message')}"
                    )

                return msg.get("result", {})

    async def _send_raw(self, obj: dict) -> None:
        self._proc.stdin.write((json.dumps(obj) + "\n").encode())
        await self._proc.stdin.drain()

    # ── Schema translation ────────────────────────────────────────────────────

    @staticmethod
    def _to_azure_tool(mcp_tool: dict) -> dict:
        """Convert one MCP tool definition → Azure OpenAI function definition."""
        schema = mcp_tool.get("inputSchema", {"type": "object", "properties": {}})
        # Azure OpenAI requires the root schema to be type=object
        if schema.get("type") != "object":
            schema = {"type": "object", "properties": {}}
        return {
            "type": "function",
            "function": {
                "name":        mcp_tool["name"],
                "description": mcp_tool.get("description", ""),
                "parameters":  schema,
            },
        }

    # ── Result parsing ────────────────────────────────────────────────────────

    @staticmethod
    def _parse_result(raw: dict) -> dict:
        """Flatten the MCP content array into a usable dict.

        MCP tools return:
            {"content": [{"type": "text", "text": "..."}, ...]}
        Screenshot tools add:
            {"type": "image", "data": "<base64>", "mimeType": "image/png"}
        Snapshot tools add a resource entry with the accessibility tree.
        """
        content   = raw.get("content", [])
        texts: list[str] = []
        shot_b64: Optional[str] = None

        for item in content:
            kind = item.get("type", "")
            if kind == "text":
                texts.append(item.get("text", ""))
            elif kind == "image":
                shot_b64 = item.get("data", "")
            elif kind == "resource":
                # Snapshot returns accessibility tree as a resource blob
                res = item.get("resource", {})
                texts.append(res.get("text", ""))

        return {
            "text":           "\n".join(texts),
            "screenshot_b64": shot_b64,
            "content":        content,
        }
