#!/usr/bin/env node
/**
 * Validate a Playwright test by copying to tests/ and running
 *
 * Usage: node validate-test.js <test-file-path>
 * Output: JSON with { status, error, duration, passed }
 */

const { execFile } = require('child_process');
const fs = require('fs');
const path = require('path');

const testFile = process.argv[2];

if (!testFile) {
  console.error(JSON.stringify({
    status: 'ERROR',
    error: 'Test file path required as argument',
    duration: 0
  }));
  process.exit(1);
}

if (!fs.existsSync(testFile)) {
  console.error(JSON.stringify({
    status: 'ERROR',
    error: `Test file not found: ${testFile}`,
    duration: 0
  }));
  process.exit(1);
}

const startTime = Date.now();

// Find the most recently written PNG inside test-results/ that was created
// after `sinceMs`. Playwright saves failure screenshots there automatically
// when screenshot: 'only-on-failure' is set in playwright.config.ts.
function findFailureScreenshot(testResultsDir, sinceMs) {
  if (!fs.existsSync(testResultsDir)) return null;
  let latest = null;
  let latestMtime = sinceMs; // only files newer than the run start
  function walk(dir) {
    let entries;
    try { entries = fs.readdirSync(dir, { withFileTypes: true }); } catch { return; }
    for (const entry of entries) {
      const full = path.join(dir, entry.name);
      if (entry.isDirectory()) { walk(full); }
      else if (entry.name.endsWith('.png')) {
        try {
          const mtime = fs.statSync(full).mtimeMs;
          if (mtime > latestMtime) { latestMtime = mtime; latest = full; }
        } catch { /* ignore */ }
      }
    }
  }
  walk(testResultsDir);
  return latest;
}

// Find the tests directory (should be in project root)
const projectRoot = process.cwd();
const testsDir = path.join(projectRoot, 'tests');

// Create tests dir if it doesn't exist
if (!fs.existsSync(testsDir)) {
  fs.mkdirSync(testsDir, { recursive: true });
}

// Copy the test file to tests/validate-temp.spec.ts
const tempTestName = 'validate-temp.spec.ts';
const tempTestPath = path.join(testsDir, tempTestName);

try {
  const testContent = fs.readFileSync(testFile, 'utf-8');
  fs.writeFileSync(tempTestPath, testContent);

  console.log(`[validate] Copied ${testFile} to ${tempTestPath}`);

  // Run playwright test
  const args = ['playwright', 'test', tempTestName, '--reporter=json'];

  const env = Object.assign({}, process.env);
  delete env.LOCAL_VALIDATION; // ensure headless=true (never open a browser window)

  execFile('npx', args,
    {
      timeout: 65000,
      cwd: projectRoot,
      maxBuffer: 10 * 1024 * 1024,
      env: env,
    },
    (error, stdout, stderr) => {
      const duration = ((Date.now() - startTime) / 1000).toFixed(2);

      // Clean up temp file
      try {
        fs.unlinkSync(tempTestPath);
        console.log(`[validate] Cleaned up ${tempTestPath}`);
      } catch (e) {
        console.log(`[validate] Warning: Could not delete temp file: ${e.message}`);
      }

      const passed = error === null || (error && error.code === 0);
      const status = passed ? 'PASS' : 'FAIL';

      let errorMessage = null;
      let jsonOutput = null;

      // Parse JSON output — Playwright's JSON reporter dumps a giant single-line
      // JSON blob to stdout, so we can't just "find the first {-line". Instead,
      // pick the *last* line that looks like a complete JSON object. Our own
      // result line (emitted at the bottom of this script) will always win.
      try {
        const lines = (stdout || '').split('\n').map(l => l.trim());
        for (let i = lines.length - 1; i >= 0; i--) {
          const line = lines[i];
          if (line.startsWith('{') && line.endsWith('}')) {
            try {
              jsonOutput = JSON.parse(line);
              break;
            } catch { /* not parseable — keep searching */ }
          }
        }
      } catch (e) {
        // JSON parse failed
      }

      // Extract error if test failed
      if (!passed) {
        if (jsonOutput?.suites?.[0]?.tests?.[0]?.error?.message) {
          errorMessage = jsonOutput.suites[0].tests[0].error.message;
        } else if (stderr && stderr.trim()) {
          errorMessage = stderr.split('\n')[0].trim();
        } else if (stdout && stdout.trim()) {
          const errorMatch = stdout.match(/Error: (.+)/);
          if (errorMatch) {
            errorMessage = errorMatch[1];
          } else {
            const lines = stdout.split('\n').filter(l => l.trim() && !l.includes('Playwright'));
            errorMessage = lines[0]?.trim() || 'Test failed without error message';
          }
        }
      }

      const result = {
        status,
        error: errorMessage,
        duration: parseFloat(duration),
        passed: passed,
        timestamp: new Date().toISOString()
      };

      // Pass the failure screenshot PATH (not base64) so Phase 2 can read it.
      // Embedding base64 in stdout creates a 500KB+ JSON line that breaks parsing.
      if (!passed) {
        const testResultsDir = path.join(projectRoot, 'test-results');
        const screenshotPath = findFailureScreenshot(testResultsDir, startTime);
        if (screenshotPath) {
          result.failureScreenshotPath = screenshotPath;
          console.log(`[validate] Failure screenshot found (${Math.round(fs.statSync(screenshotPath).size / 1024)}KB): ${screenshotPath}`);
        } else {
          console.log('[validate] No failure screenshot found in test-results/');
        }
      }

      console.log(JSON.stringify(result));
      process.exit(passed ? 0 : 1);
    }
  );

} catch (err) {
  const duration = ((Date.now() - startTime) / 1000).toFixed(2);

  // Clean up if error during setup
  try {
    fs.unlinkSync(tempTestPath);
  } catch (e) {}

  console.error(JSON.stringify({
    status: 'ERROR',
    error: `Setup error: ${err.message}`,
    duration: parseFloat(duration),
    passed: false
  }));
  process.exit(1);
}
