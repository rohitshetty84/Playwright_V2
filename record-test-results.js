#!/usr/bin/env node
/**
 * Record Playwright Test Results to Studio API
 *
 * This script:
 * 1. Runs your Playwright tests
 * 2. Parses the test results
 * 3. Posts them to Studio API for Run History
 *
 * Usage:
 *   node record-test-results.js [golden_id] [golden_name]
 *
 * Example:
 *   node record-test-results.js onboarding-test "onboarding.spec.ts"
 *   node record-test-results.js navigate-wiki "navigate-to-wikipediaorg"
 */

const fs = require("fs");
const path = require("path");
const { execSync } = require("child_process");
const http = require("http");

const STUDIO_API = process.env.STUDIO_URL || "http://localhost:8000";
const GOLDEN_ID = process.argv[2];
const GOLDEN_NAME = process.argv[3];

// ============================================================================
// Validation
// ============================================================================

if (!GOLDEN_ID || !GOLDEN_NAME) {
  console.error("❌ Usage: node record-test-results.js <golden_id> <golden_name>");
  console.error("");
  console.error("Example:");
  console.error('  node record-test-results.js onboarding-test "onboarding.spec.ts"');
  console.error('  node record-test-results.js navigate-wiki "navigate-to-wikipediaorg"');
  process.exit(1);
}

console.log("📊 Playwright Test Result Recorder");
console.log("====================================");
console.log(`Golden ID: ${GOLDEN_ID}`);
console.log(`Golden Name: ${GOLDEN_NAME}`);
console.log(`Studio API: ${STUDIO_API}`);
console.log("");

// ============================================================================
// Step 1: Run Tests
// ============================================================================

console.log("🏃 Running Playwright tests...");
console.log("");

try {
  execSync("npx playwright test --reporter=json > test-results/results.json 2>&1", {
    stdio: "inherit",
    shell: true,
  });
} catch (e) {
  // Tests may fail, that's OK - we still want to record the results
  console.log("(Tests may have failed - that's OK, we'll still record results)");
}

console.log("");

// ============================================================================
// Step 2: Parse Results
// ============================================================================

console.log("📖 Parsing test results...");

const resultsPath = path.join(process.cwd(), "test-results", "results.json");

if (!fs.existsSync(resultsPath)) {
  console.error("❌ Error: Could not find test-results/results.json");
  console.error("Make sure you're running from the project root directory");
  process.exit(1);
}

let results;
try {
  const resultsJson = fs.readFileSync(resultsPath, "utf-8");
  results = JSON.parse(resultsJson);
} catch (e) {
  console.error("❌ Error parsing results.json:", e.message);
  process.exit(1);
}

// ============================================================================
// Step 3: Extract Candidates
// ============================================================================

const candidates = [];

if (results.suites && Array.isArray(results.suites)) {
  for (const suite of results.suites) {
    if (suite.tests && Array.isArray(suite.tests)) {
      for (const test of suite.tests) {
        const candidate = {
          name: test.title,
          path: extractPath(test.title) || "A",
          status: test.status === "passed" ? "pass" : "fail",
          duration: `${test.duration}ms`,
        };

        // Add error if failed
        if (test.status !== "passed" && test.error) {
          const errorMsg = test.error.message || String(test.error);
          candidate.error = errorMsg.substring(0, 200); // Truncate to 200 chars
        }

        candidates.push(candidate);
      }
    }
  }
}

console.log(`✓ Found ${candidates.length} test results`);
for (const c of candidates) {
  const badge = c.status === "pass" ? "✅" : "❌";
  console.log(`  ${badge} ${c.name} (${c.status})`);
  if (c.error) {
    console.log(`     Error: ${c.error.substring(0, 80)}...`);
  }
}
console.log("");

if (candidates.length === 0) {
  console.warn("⚠️  No test results found");
}

// ============================================================================
// Step 4: Prepare Payload
// ============================================================================

console.log("📦 Preparing API payload...");

const payload = {
  golden_id: GOLDEN_ID,
  browser: "msedge", // Could be parameterized
  candidates: candidates,
};

console.log("Payload:", JSON.stringify(payload, null, 2));
console.log("");

// ============================================================================
// Step 5: Post to API
// ============================================================================

console.log(`📤 Posting results to ${STUDIO_API}/api/runs...`);

postToAPI(payload, (error, response) => {
  if (error) {
    console.error("❌ Failed to post results:", error);
    console.error("");
    console.error("Troubleshooting:");
    console.error("1. Is Studio running? Run: python server.py");
    console.error("2. Is STUDIO_URL correct? Current:", STUDIO_API);
    console.error("3. Check Studio logs for errors");
    process.exit(1);
  }

  console.log("✅ Results posted successfully!");
  console.log("");
  console.log("📋 Next steps:");
  console.log("1. Open Studio: http://localhost:8000");
  console.log("2. Go to 'Run History' tab");
  console.log("3. You should see the new run with results");
  console.log("4. If tests failed, go to 'Auto-Heal' to fix them");
  console.log("");
});

// ============================================================================
// Helper Functions
// ============================================================================

function extractPath(testTitle) {
  // Try to extract path from test title (e.g., "[Path A]" -> "A")
  const match = testTitle.match(/\[Path ([A-Z])\]/i);
  return match ? match[1] : null;
}

function postToAPI(data, callback) {
  const url = new URL(STUDIO_API);
  const postData = JSON.stringify(data);

  const options = {
    hostname: url.hostname,
    port: url.port,
    path: "/api/runs",
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "Content-Length": Buffer.byteLength(postData),
    },
  };

  const req = http.request(options, (res) => {
    let body = "";

    res.on("data", (chunk) => {
      body += chunk;
    });

    res.on("end", () => {
      if (res.statusCode === 200) {
        callback(null, body);
      } else {
        callback(`HTTP ${res.statusCode}: ${body}`);
      }
    });
  });

  req.on("error", (e) => {
    callback(e);
  });

  req.write(postData);
  req.end();
}
