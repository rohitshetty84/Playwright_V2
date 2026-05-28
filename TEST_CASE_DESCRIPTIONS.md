# PLAYWRIGHT AI STUDIO - TEST CASE DESCRIPTIONS
## For Testing via Internet (Public Websites)

These test cases can be copy-pasted into the Studio's "TEST CASE DESCRIPTION" field to generate and test automated scripts.

---

## 📌 TEST CASE 1: GitHub Repository Search & Navigation

### Category: Developer Tools / Search
### Difficulty: Medium
### Estimated Time: 3-5 minutes

### Test Case Description
```
Navigate to GitHub (github.com), search for "playwright automation" in the search box, 
click on the first search result that is a repository, verify that the repository page 
loads and shows the repository name, star count, and description. Finally, click on the 
"Code" button and verify that the code view is displayed.
```

### Why This Works for Testing
- ✅ GitHub is stable and public
- ✅ Involves search functionality
- ✅ Multiple element interactions
- ✅ Good for testing selector patterns
- ✅ Can trigger healing if selectors are incorrect
- ✅ No authentication required

### Expected Issues (For Healing Testing)
- Selector changes in GitHub UI
- Dynamic element loading
- Navigation timing issues

### Success Criteria
✓ Search box found and filled  
✓ Search results loaded  
✓ Repository link clicked  
✓ Repository page verified  
✓ Code button found and clicked  

---

## 📌 TEST CASE 2: Stack Overflow Question Search

### Category: Q&A Platform / Search
### Difficulty: Medium
### Estimated Time: 3-5 minutes

### Test Case Description
```
Open Stack Overflow (stackoverflow.com), use the search bar to search for 
"playwright javascript testing", wait for search results to load, click on the first 
question result, verify the question title is displayed, and confirm that the question 
has at least one answer by checking for the answer section.
```

### Why This Works for Testing
- ✅ Stack Overflow is stable
- ✅ Tests search and filtering
- ✅ Page load variations possible
- ✅ Multiple elements to interact with
- ✅ Good for testing async operations
- ✅ No login required

### Expected Issues (For Healing Testing)
- Dynamic page content
- Lazy loading of answers
- Selector specificity issues
- Navigation timing

### Success Criteria
✓ Search initiated successfully  
✓ Results displayed  
✓ First question clicked  
✓ Question title visible  
✓ Answer section confirmed  

---

## 📌 TEST CASE 3: Wikipedia Article Navigation

### Category: Information / Navigation
### Difficulty: Easy to Medium
### Estimated Time: 2-4 minutes

### Test Case Description
```
Go to Wikipedia (wikipedia.org), search for "Test Automation" using the search box 
located at the top of the page, select the first search result from the dropdown, 
verify that you are on the Test Automation article page by checking for the page title, 
and click on the "History" tab to view the article edit history.
```

### Why This Works for Testing
- ✅ Wikipedia highly stable
- ✅ Simple search mechanism
- ✅ Predictable structure
- ✅ Good baseline test
- ✅ Tests navigation tabs
- ✅ No auth required

### Expected Issues (For Healing Testing)
- Search box selector variations
- Tab selector specificity
- Page transition timing
- Element visibility timing

### Success Criteria
✓ Search box located  
✓ Search term entered  
✓ Result selected  
✓ Article page verified  
✓ History tab clicked  
✓ History content displayed  

---

## 📌 TEST CASE 4: Amazon Product Search & Details

### Category: E-commerce / Search
### Difficulty: Medium
### Estimated Time: 4-6 minutes

### Test Case Description
```
Visit Amazon (amazon.com), use the search bar to search for "laptop", 
apply a filter for "Under $500" if available, click on the first product in the results, 
verify the product page loads showing the product title, price, and star rating, 
and add the product to the wishlist (or cart if you prefer not to add to wishlist).
```

### Why This Works for Testing
- ✅ Amazon is heavily used
- ✅ Multiple interaction types
- ✅ Filter and search logic
- ✅ Complex page structure
- ✅ Good for testing selectors
- ✅ Dynamic pricing elements

### Expected Issues (For Healing Testing)
- Dynamic content loading
- Region-specific selectors
- Price format changes
- Popup elements
- Filter variation per search term

### Success Criteria
✓ Search executed  
✓ Results displayed  
✓ Filter applied  
✓ Product selected  
✓ Product details verified  
✓ Wishlist action completed  

---

## 📌 TEST CASE 5: LinkedIn Job Search

### Category: Professional Network / Search
### Difficulty: Medium to Hard
### Estimated Time: 4-6 minutes

### Test Case Description
```
Go to LinkedIn Jobs (linkedin.com/jobs/search), search for "QA Engineer", 
set the location filter to "United States", apply the filter, click on the first 
job posting in the results, verify that the job details panel opens showing the 
job title, company name, and job description, and click the "Save" or "Easy Apply" 
button to interact with the job posting.
```

### Why This Works for Testing
- ✅ LinkedIn is stable
- ✅ Tests filtering logic
- ✅ Multiple form interactions
- ✅ Dynamic content loading
- ✅ Good for testing waits
- ✅ Location selection test

### Expected Issues (For Healing Testing)
- Modal/panel opening animations
- Filter dropdown complexity
- Element visibility timing
- Dynamic job listing
- Button selector variations

### Success Criteria
✓ Search entered  
✓ Location filter applied  
✓ Results filtered  
✓ Job posting clicked  
✓ Job details displayed  
✓ Save/Apply button clicked  

---

## 📌 TEST CASE 6: Google Search & Knowledge Panel

### Category: Search Engine
### Difficulty: Easy
### Estimated Time: 2-3 minutes

### Test Case Description
```
Open Google Search (google.com), search for "Playwright automation tool", 
verify that search results are displayed, locate the knowledge panel on the right 
side (if available), click on the official Playwright website link from the results, 
and verify that you are on the Playwright documentation page.
```

### Why This Works for Testing
- ✅ Google is most stable
- ✅ Simple test case
- ✅ Good baseline
- ✅ Tests search functionality
- ✅ Tests link navigation
- ✅ No auth needed

### Expected Issues (For Healing Testing)
- Knowledge panel not always present
- Search result order variation
- Sponsored results position
- Mobile vs desktop layout
- Cookie consent handling

### Success Criteria
✓ Search performed  
✓ Results loaded  
✓ Official link found  
✓ Playwright page opened  
✓ Documentation verified  

---

## 📌 TEST CASE 7: GitHub Code Search & File View

### Category: Developer Tools / Code Search
### Difficulty: Medium to Hard
### Estimated Time: 4-6 minutes

### Test Case Description
```
Go to GitHub Advanced Search (github.com/search/advanced), search for repositories 
with "playwright" in the name, set language to "TypeScript", click the search button, 
verify that the search results show TypeScript-based Playwright repositories, click on 
the first result, navigate to the README file, and verify that the README content is 
displayed with proper formatting.
```

### Why This Works for Testing
- ✅ GitHub advanced search
- ✅ Multiple filter interactions
- ✅ Complex form handling
- ✅ File navigation
- ✅ Content verification
- ✅ Good for testing waits

### Expected Issues (For Healing Testing)
- Form field selectors
- Dropdown interactions
- Search result loading time
- File rendering
- Dynamic syntax highlighting

### Success Criteria
✓ Advanced search opened  
✓ Search terms entered  
✓ Filter applied  
✓ Search executed  
✓ Repository found  
✓ README displayed  

---

## 📌 TEST CASE 8: MDN Web Docs Search

### Category: Documentation / Search
### Difficulty: Easy to Medium
### Estimated Time: 2-4 minutes

### Test Case Description
```
Visit MDN Web Docs (developer.mozilla.org), click on the search box at the top, 
search for "JavaScript async await", click on the first search result, verify that 
the documentation page loads and displays the article title, and scroll down to find 
and click on the "Syntax" section to verify that code examples are displayed.
```

### Why This Works for Testing
- ✅ MDN is highly stable
- ✅ Good search implementation
- ✅ Tests scrolling
- ✅ Section navigation
- ✅ Code block verification
- ✅ No auth needed

### Expected Issues (For Healing Testing)
- Search box selector location
- Dynamic search suggestions
- Section anchor links
- Scroll timing
- Code block rendering

### Success Criteria
✓ Search initiated  
✓ Results displayed  
✓ Article opened  
✓ Title verified  
✓ Scrolled successfully  
✓ Syntax section found  
✓ Code examples visible  

---

## 📌 TEST CASE 9: PyPI Package Search

### Category: Package Repository / Search
### Difficulty: Medium
### Estimated Time: 3-5 minutes

### Test Case Description
```
Navigate to PyPI (pypi.org), search for "pytest" in the search bar, click on the 
first result which should be the pytest package, verify that the package page displays 
the package name, version number, and description, click on the "Project Links" section, 
and verify that at least one project link (like GitHub or Documentation) is displayed.
```

### Why This Works for Testing
- ✅ PyPI is stable
- ✅ Simple package repository
- ✅ Consistent structure
- ✅ Version display testing
- ✅ Link verification
- ✅ No auth required

### Expected Issues (For Healing Testing)
- Search result ordering
- Package page structure
- Link availability
- Section expansion
- Dynamic content loading

### Success Criteria
✓ Search performed  
✓ Package page loaded  
✓ Package details verified  
✓ Version displayed  
✓ Description shown  
✓ Project links found  

---

## 📌 TEST CASE 10: Twitter/X Tweet Search

### Category: Social Media / Search
### Difficulty: Medium to Hard
### Estimated Time: 4-6 minutes

### Test Case Description
```
Open Twitter/X (twitter.com), click on the search bar at the top, search for 
"Playwright testing", view the search results, click on the "Top" tab to filter 
for popular tweets, click on the first tweet result, verify that the tweet details 
page opens showing the tweet text and engagement metrics (likes, retweets), and click 
on the "Reply" or comment button.
```

### Why This Works for Testing
- ✅ Twitter is widely used
- ✅ Multiple filter tabs
- ✅ Dynamic content
- ✅ Infinite scroll testing
- ✅ Modal interactions
- ⚠️ Requires handling login (but guest view possible)

### Expected Issues (For Healing Testing)
- Dynamic content loading
- Tab switching timing
- Modal animations
- Like/retweet button states
- Scroll behavior
- Authentication state

### Success Criteria
✓ Search executed  
✓ Results displayed  
✓ Tab filtered  
✓ Tweet clicked  
✓ Tweet details displayed  
✓ Engagement metrics visible  
✓ Reply action triggered  

---

## 🎯 RECOMMENDED TEST ORDER FOR YOUR HEALING ENGINE

### Level 1: Easy Tests (Start Here)
1. **Google Search** - Simplest, most stable
2. **Wikipedia Search** - Straightforward, reliable
3. **MDN Docs Search** - Predictable structure

### Level 2: Medium Tests (For Healing Practice)
4. **Stack Overflow Search** - Tests filtering
5. **GitHub Search** - Tests complex navigation
6. **PyPI Package Search** - Tests link handling

### Level 3: Advanced Tests (For Challenging Cases)
7. **LinkedIn Jobs** - Tests forms and modals
8. **Amazon Search** - Tests dynamic pricing
9. **GitHub Code Search** - Tests advanced filters
10. **Twitter Search** - Tests infinite scroll/modals

---

## 🚀 HOW TO USE THESE TEST CASES IN STUDIO

### Step 1: Open Studio
Go to http://localhost:8000/

### Step 2: Click "Synthesize"
Select the Synthesize tab in the left sidebar

### Step 3: Enter Test Case Description
Copy one of the test cases above and paste it into the "TEST CASE DESCRIPTION" field

### Step 4: Click "Synthesize"
Wait for the AI to generate the test script

### Step 5: Save as Golden
If satisfied with generated script, click "Save as Golden"

### Step 6: Run or Test Locally
- In the "Auto-Heal" tab, click "Validate Fix (Local)" to test immediately
- Or click "Promote" and let GitHub Actions test it

---

## 💡 TESTING TIPS FOR YOUR HEALING ENGINE

### For Testing TIMING_RACE Errors
Use test cases with:
- Dynamic content loading
- Slow pages (Amazon)
- Infinite scroll (Twitter)
- Modal animations

### For Testing SELECTOR_MIXING Errors
Use test cases that generate code mixing:
- getByRole with locator
- getByLabel with locator
- getByText with locator

### For Testing LOGIN_CORRUPTION
These public test cases won't have auth, but your healing engine can still:
- Recognize if code tries to add cookies before goto()
- Practice fixing that pattern
- Test on your own auth scenarios later

---

## 📋 QUICK REFERENCE

**Easiest Tests:**
- Google Search
- Wikipedia Article
- MDN Docs

**Most Interactive:**
- Amazon Product
- LinkedIn Jobs
- Twitter Tweet

**Best for Selectors:**
- Stack Overflow
- GitHub Repository
- PyPI Package

**Best for Complex Flow:**
- GitHub Advanced Search
- LinkedIn Job Search
- Amazon Product with Filter

---

## ⚠️ IMPORTANT NOTES

1. **No Authentication Required** - All these sites work without login (except optional LinkedIn features)
2. **Internet Connection Needed** - These test cases require internet access
3. **Dynamic Content** - Results may vary by region/time, making them good for testing healing
4. **Realistic Scenarios** - These mirror real user workflows
5. **Good for Regression** - Once generated, these tests can be re-run regularly

---

## 🎬 NEXT STEPS

### To Test Your Healing Engine:

1. **Generate a test** using any of the above descriptions
2. **The test might fail** because generated selectors may not match current site structure
3. **Click "Auto-Heal"** to see your improved healing engine:
   - Diagnose root cause
   - Generate targeted fix
   - Run locally (2-5 seconds)
   - Show diagnosis info
4. **Observe the diagnosis:**
   - Is it timing_race? selector_mixing? login_corruption?
   - How confident is it?
   - Does the fix make sense?

### To Practice Healing:

- Generate 2-3 tests from different categories
- Let them fail naturally (likely selector changes)
- Use Auto-Heal to fix them
- Watch how your healing engine adapts
- Test the learning system across multiple attempts

---

## 📊 EXPECTED RESULTS

### When Everything Works ✅
```
Test generates → Selectors are correct → Test passes
```

### When Healing is Needed (For Testing) ⚠️
```
Test generates → Selectors don't match → Test fails → 
Auto-Heal diagnoses issue → Generates fix → Validates locally → 
Result: PASS or FAIL with diagnosis info
```

---

## 📞 TROUBLESHOOTING

| Issue | Solution |
|-------|----------|
| "Element not found" | Auto-Heal will diagnose timing_race → add waits |
| "Mixing selectors" | Auto-Heal will diagnose selector_mixing → normalize |
| "Wrong selector" | This is normal - sites update their UI. Healing will fix it |
| "Timeout error" | Test needs more explicit waits. Healing will add them |
| "Modal not found" | Dynamic element. Healing will add proper waits |

---

## ✨ WHAT YOU'LL LEARN

By using these test cases:
✅ How Studio generates tests from descriptions  
✅ How the healing engine diagnoses errors  
✅ How confident scoring helps prioritize fixes  
✅ How learning adapts to different root causes  
✅ Real-world test automation patterns  

**All with publicly accessible websites!**

