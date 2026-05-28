# HOW TO TEST YOUR PLAYWRIGHT AI STUDIO WITH INTERNET TEST CASES

## 🎯 GOAL
Generate real Playwright tests from simple descriptions, then test your **improved healing engine** when tests fail.

---

## 🚀 QUICK START (5 MINUTES)

### Step 1: Open Studio
```
Go to: http://localhost:8000
```

### Step 2: Click "Synthesize" Tab
Look at the left sidebar and click on "Synthesize"

### Step 3: Copy Test Case
Pick the easiest one to start:

**GOOGLE SEARCH TEST:**
```
Open Google Search (google.com), search for "Playwright automation tool", 
verify that search results are displayed, locate the knowledge panel on the right 
side if available, click on the official Playwright website link from the results, 
and verify that you are on the Playwright documentation page.
```

### Step 4: Paste into Form
In the "TEST CASE DESCRIPTION" field, paste the test case above

### Step 5: Click "Synthesize"
Wait 30-60 seconds for the AI to generate the test code

### Step 6: Review Generated Code
- Check the TypeScript code that was generated
- Should have proper imports, test structure, assertions
- Notice the selectors used

### Step 7: Click "Save as Golden"
- Name it: `google-search-test`
- Click "Save as Golden"
- This saves it as a test case

---

## 📋 STEP-BY-STEP: Full Example

### Scenario: Testing Wikipedia Navigation

```
WHAT YOU DO                          WHAT STUDIO DOES
─────────────────────────────────────────────────────────────

1. Go to http://localhost:8000 ───→ Studio loads

2. Click "Synthesize" tab ─────────→ Shows test generation form

3. Copy this text:
   "Go to Wikipedia (wikipedia.org), 
    search for 'Test Automation', 
    verify article loads, 
    click History tab"

4. Paste into description field ───→ Form shows text

5. Click "Synthesize" ──────────────→ AI generates test code

   [Generating test from description...]
   [Calling Azure OpenAI...]
   [Creating TypeScript code...]

6. Review the code ─────────────────→ You see:

   import { test, expect } from '@playwright/test';
   
   test('Navigate Wikipedia and view history', async ({ page }) => {
     await page.goto('https://www.wikipedia.org');
     await page.waitForLoadState('networkidle');
     const searchBox = page.locator('input[name="search"]');
     await searchBox.fill('Test Automation');
     await page.locator('button:has-text("Search")').click();
     const heading = page.locator('h1');
     await expect(heading).toContainText('Test Automation');
     await page.locator('[aria-label="View history"]').click();
   });

7. Click "Save as Golden" ──────────→ Test saved with ID like: a1b2c3d4

8. Now in "Auto-Heal" tab, click 
   "Validate Fix (Local)" ─────────→ Test runs locally (2-5 seconds)

9. If PASS: ✅ 
   If FAIL: Shows error, click 
   "Auto-Heal" ───────────────────→ Healing engine:
                                    1. Diagnoses root cause
                                    2. Generates targeted fix
                                    3. Validates fix locally
                                    4. Shows diagnosis info
```

---

## 🔄 COMPLETE WORKFLOW

```
START
  ↓
[1] Open Studio → http://localhost:8000
  ↓
[2] Go to "Synthesize" tab
  ↓
[3] Paste test case description
  ↓
[4] Click "Synthesize"
  ↓
[5] AI generates TypeScript test
  ↓
[6] Click "Save as Golden"
  ↓
[7] Go to "Auto-Heal" tab (or click test result)
  ↓
[8] Click "Validate Fix (Local)"
  ↓
   ┌─────────────────────────────────────┐
   │  TEST RUNS LOCALLY (2-5 seconds)    │
   └─────────────────────────────────────┘
  ↓
   ┌──────────────┐  ┌──────────────────────────┐
   │ TEST PASSES? │  │ HEALING ENGINE IN ACTION │
   └──────────────┘  └──────────────────────────┘
        ↓ YES            ↓ NO
   ┌─────────────────────────────────────┐
   │ ✅ GREAT!                           │
   │ Click "Promote" to save healed code │
   │ Ready for GitHub Actions             │
   └─────────────────────────────────────┘
        
        ↓ IF NO (Test fails - good for testing healing!)
   ┌─────────────────────────────────────┐
   │ ❌ TEST FAILED                      │
   │ Shows error: "Locators must belong   │
   │ to same frame" (or other error)      │
   │                                      │
   │ Click "Auto-Heal" button ────┐       │
   └─────────────────────────────────────┘
                                  │
                                  ↓
                    ┌──────────────────────────────┐
                    │ HEALING ENGINE WORKS          │
                    │                              │
                    │ 1. Diagnoses root cause      │
                    │    (timing_race / selector_  │
                    │     mixing / login_          │
                    │     corruption)              │
                    │                              │
                    │ 2. Shows confidence %        │
                    │    (60% / 85% / 95%)         │
                    │                              │
                    │ 3. Generates targeted fix    │
                    │                              │
                    │ 4. Tests fix locally         │
                    │    (2-5 seconds)             │
                    │                              │
                    │ 5. Shows results with        │
                    │    diagnosis info            │
                    └──────────────────────────────┘
                                  │
                    ┌─────────────────────────────────────┐
                    │ RESULT SHOWN                         │
                    │                                     │
                    │ {                                   │
                    │   "testResult": "PASS or FAIL",     │
                    │   "diagnosis": {                    │
                    │     "rootCause": "selector_mixing", │
                    │     "confidence": 0.85,             │
                    │     "evidence": "Found mixed..."    │
                    │   }                                 │
                    │ }                                   │
                    └─────────────────────────────────────┘
                                  │
                        ┌─────────────────────┐
                        │ CLICK "PROMOTE" OR  │
                        │ TRY AUTO-HEAL AGAIN │
                        └─────────────────────┘
                                  │
                                  ↓
                            END
```

---

## 📊 WHAT YOU'LL SEE AT EACH STEP

### After Clicking "Synthesize"

```
STUDIO GENERATES:

✓ Playwright test file with proper structure
✓ Import statements
✓ Test description
✓ Navigation step (page.goto)
✓ Waits (page.waitForLoadState)
✓ Element selectors (page.locator)
✓ Interactions (fill, click)
✓ Assertions (expect)
✓ Console logs for tracking
```

### After Clicking "Validate Fix (Local)"

```
STUDIO RUNS TEST:

[Running test locally...]
[Copied test to: /tests/validate-temp.spec.ts]
[Running: npx playwright test]

POSSIBLE RESULTS:

✅ PASS - All steps completed
   ✓ Navigated to page
   ✓ Element found
   ✓ Interaction successful
   ✓ Assertion passed

❌ FAIL - Something went wrong
   Error: "Locators must belong to the same frame"
   or
   Error: "Element not found"
   or
   Error: "Timeout waiting for selector"
```

### After Clicking "Auto-Heal"

```
HEALING ENGINE RUNS:

[Step 1] Analyzing error...
  Error: "Locators must belong to the same frame"

[Step 2] Diagnosing root cause...
  Root Cause: selector_mixing (85% confidence)
  Evidence: Found mixed selector types in .or() chains

[Step 3] Generating targeted fix...
  Strategy: Normalize all selectors to page.locator()

[Step 4] Creating healed code...
  ✓ Added [AI-HEAL] comments
  ✓ Changed getByRole to page.locator
  ✓ Normalized .or() chains
  ✓ Added .first() for disambiguation

[Step 5] Testing healed code locally...
  Running: npx playwright test (healed version)

[Step 6] Validating results...
  ✅ TEST PASSED!
  or
  ❌ Test still failing - showing new diagnosis
```

---

## 💡 TESTING TIPS

### Tip 1: Start with Easiest Test Cases
```
EASIEST:        MEDIUM:         HARDEST:
Google Search   Stack Overflow  LinkedIn Jobs
Wikipedia       GitHub Search   Amazon Product
MDN Docs        PyPI Package    Twitter Search
```

### Tip 2: Let Tests Fail Naturally
```
This is GOOD for testing healing engine!

Expected flow:
1. Generate test → Selectors might be outdated
2. Test runs → Fails because site changed
3. Auto-Heal → Engine diagnoses issue
4. Healed test → Runs and passes
5. Promoted → Ready for production
```

### Tip 3: Watch the Diagnosis
```
Each failure will show:
- rootCause: What actually went wrong
- confidence: How sure the engine is
- evidence: Why it was diagnosed that way

Learn to recognize:
✓ timing_race (60%): Need more waits
✓ selector_mixing (85%): Mixed getByRole + locator
✓ login_corruption (95%): Auth before navigation
```

### Tip 4: Review the Healed Code
```
Look for [AI-HEAL] comments explaining changes:

// [AI-HEAL] Changed getByRole to page.locator 
// to avoid mixing selector types in .or() chains
const button = page.locator('button[type="submit"]')
  .or(() => page.locator('button:has-text("Save")'))
  .first();  // [AI-HEAL] Added .first() to disambiguate
```

### Tip 5: Check Healing History
```
After each healing attempt, check:
/playwright-ai-studio/healing_history/[golden_id]_history.json

You'll see:
- attemptNumber: Which attempt this was
- rootCause: What was diagnosed
- confidence: How sure the system was
- succeeded: Did the fix work?
- timestamp: When it happened
```

---

## 🎯 RECOMMENDED TEST SEQUENCE

### SESSION 1: Learning (15 minutes)

```
STEP 1: Google Search Test (Easiest)
├─ Copy test case
├─ Synthesize
├─ Review code
└─ Save as Golden

STEP 2: Wikipedia Test (Also Easy)
├─ Copy test case
├─ Synthesize
├─ Review code
└─ Save as Golden

STEP 3: Validate Both
├─ Click "Validate Fix (Local)"
├─ Both should PASS (if selectors are current)
└─ Great! Your Studio works!
```

### SESSION 2: Testing Healing (30 minutes)

```
STEP 1: Stack Overflow Test
├─ Copy test case
├─ Synthesize
├─ Save as Golden
└─ Click "Validate Fix (Local)"

STEP 2: Test Likely Fails
├─ Observe error
├─ Click "Auto-Heal"
├─ Watch healing engine work
├─ See diagnosis (should be selector_mixing or timing_race)
├─ Review healed code
└─ See test pass (or understand why it didn't)

STEP 3: Promote Healed Code
├─ Click "Promote"
├─ Code saved as new golden
└─ Ready for GitHub Actions
```

### SESSION 3: Advanced Learning (45 minutes)

```
STEP 1: Generate Same Test 3 Times
├─ Use MDN Docs test
├─ Generate, let fail, Auto-Heal
├─ Generate again, let fail, Auto-Heal
├─ Generate 3rd time, Auto-Heal
└─ Watch learning improve each attempt

STEP 2: Compare Results
├─ Check healing_history JSON
├─ See each attempt's diagnosis
├─ Notice confidence scores
├─ Observe how fixes improved

STEP 3: Try Different Test Type
├─ Use LinkedIn Jobs (more complex)
├─ Generate, validate, observe
├─ Let it fail and Auto-Heal
├─ See how engine handles different errors
└─ Verify diagnosis accuracy
```

---

## ✨ WHAT YOU'LL LEARN

By following this guide:

✅ **How to use Studio** - Generate tests from descriptions  
✅ **How AI generates tests** - See the synthesis process  
✅ **How healing works** - Watch diagnosis and fixing  
✅ **How learning adapts** - See system improve  
✅ **Real patterns** - Learn actual automation best practices  
✅ **Error diagnosis** - Understand different root causes  
✅ **Confidence scoring** - Know when to trust vs escalate  

---

## 🎬 READY TO START?

### Right Now:

1. Go to: **http://localhost:8000**
2. Click: **Synthesize**
3. Copy this: **"Open Google Search (google.com), search for 'Playwright automation tool', verify search results, click on the official Playwright link, verify you're on the documentation page."**
4. Paste into form
5. Click: **Synthesize**
6. Wait for code to generate (30-60 seconds)
7. Click: **Save as Golden**
8. Go to **Auto-Heal** tab
9. Click: **Validate Fix (Local)**
10. Watch your test run!

**That's it! You're now testing Studio with real internet websites!**

---

## 📞 QUICK REFERENCE

| Action | What Happens |
|--------|-------------|
| Click Synthesize | AI generates test code |
| Click Save as Golden | Test saved with ID |
| Click Validate Fix | Runs test locally (2-5 sec) |
| Test PASSES | ✅ Ready to promote |
| Test FAILS | Click Auto-Heal to see diagnosis |
| Click Auto-Heal | Engine diagnoses + generates fix |
| Click Promote | Save healed code as new golden |

---

## ⚠️ IMPORTANT NOTES

- ✅ **All test cases work with public websites** - No login needed
- ✅ **Tests validate locally in 2-5 seconds** - Much faster than GitHub
- ✅ **Healing engine will demonstrate capabilities** - Whether test passes or fails
- ✅ **You'll see diagnosis information** - Root cause with confidence %
- ⚠️ **Results may vary by region** - Sites show different content by location
- ⚠️ **Website structure changes** - This is GOOD for testing healing!

---

## 🎉 NEXT LEVEL: ADVANCED TESTING

Once you complete the basic sessions, try:

1. **Test Specific Root Causes**
   - Use Amazon for timing_race
   - Use Stack Overflow for selector_mixing
   - Create custom test with login for login_corruption

2. **Monitor Healing History**
   - Check JSON files
   - Track confidence trends
   - Observe learning patterns

3. **Intentionally Break Tests**
   - Edit golden code
   - Introduce bad selectors
   - See healing engine fix it

4. **Load Test Multiple Golden**
   - Generate 5+ different tests
   - Track healing success rates
   - See learning across multiple tests

---

**You're ready! Open Studio and paste your first test case!** 🚀
