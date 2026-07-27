# Setup

Do these in order. Don't skip any.

---

## Part A — Put it online (10 steps)

**1.** Download every file from the chat into one folder. Name the folder `mlb-model`.

**2.** Check that a folder called `.github` is inside it.
On Mac, press **Cmd + Shift + .** in Finder to see hidden folders.
If `.github` is missing, nothing will run.

**3.** Go to github.com. Click **New repository**.
Name it `mlb-model`. Do not tick any checkboxes. Click **Create repository**.

**4.** Open Terminal. Run these one at a time.
Replace `YOUR-USERNAME` with your GitHub username.

```
cd path/to/mlb-model
git init
git add .
git commit -m "v5"
git branch -M main
git remote add origin https://github.com/YOUR-USERNAME/mlb-model.git
git push -u origin main
```

**5.** On GitHub, open your repo. Click **Settings** (top row).

**6.** Click **Pages** in the left sidebar.
Under Branch, choose **main** and **/ (root)**. Click **Save**.

**7.** Click **Actions** in the left sidebar, then **General**.
Scroll down to "Workflow permissions".
Choose **Read and write permissions**. Click **Save**.

**8.** Click the **Actions** tab at the top of the repo.
Click **Refresh slate** in the left sidebar.
Click the **Run workflow** button on the right, then **Run workflow** again.

**9.** Wait 2 minutes. Reload the page until you see a green checkmark.

**10.** Open `https://YOUR-USERNAME.github.io/mlb-model/`

You should see game cards. **Part A is done.**

---

## Part B — Check it worked (2 steps)

**11.** On your site, click the **Advanced** tab.

**12.** Look at the table.
- All rows say **ok** → finished, ignore Part D.
- Any row says **FAILED** → go to Part D.

---

## Part C — Email alerts (5 steps, optional)

Skip this if you don't want emails.

**13.** Get a Gmail app password.
Go to `myaccount.google.com/apppasswords`.
Type "mlb model". Click **Create**. Copy the 16-character code.
(If the page won't load, turn on 2-Step Verification first.)

**14.** On GitHub: **Settings → Secrets and variables → Actions**.

**15.** Click **New repository secret** six times, adding one each time:

```
SMTP_HOST           smtp.gmail.com
SMTP_PORT           587
SMTP_USER           your@gmail.com
SMTP_PASS           the 16-character code from step 13
ALERT_FROM          your@gmail.com
ALERT_RECIPIENTS    your@gmail.com,friend@gmail.com
```

**16.** Click **Actions** tab → **Lineup alerts** → **Run workflow**.
Leave the dry_run box ticked. Click **Run workflow**.

**17.** Open the finished run, click the job, and expand
**Check for confirmed lineups**. The emails are printed there instead of sent.
Read one to confirm it looks right.

**Part C is done.** Real emails now send automatically. Nothing else to do.

---

## Part D — Only if something said FAILED

**If `pitching` says FAILED:**
Nothing to do. A backup method already ran. Your numbers are slightly rougher.

**If `kalshi` says FAILED:**
Kalshi didn't answer. Repeat step 8 in a few hours.

**If `kalshi.match` says 0 games matched:**
Run this on your computer:

```
pip install requests
python build_slate.py --debug-kalshi
```

It prints the real Kalshi ticker names. Compare them to the `ABBR` list
near the top of `build_slate.py`, fix any that differ, then push:

```
git add build_slate.py
git commit -m "fix kalshi codes"
git push
```

Then repeat step 8.

---

## Part E — What to do from now on

**Each game day:** open the site, click **Log this pick** on games you have a view on.

**Next morning:** open the **My picks** tab, mark each one **Won** or **Lost**.

**Every few weeks:** on that same tab, check the calibration table.
When the model says 65%, do those teams win about 65% of the time?
If they win 55%, the model is wrong and no edge it reports is real.

---

## If the site is blank or broken

| What you see | Do this |
|---|---|
| "No data yet" | Steps 7 and 8 |
| 404 page not found | Step 6 |
| Workflow has a red X | Step 7, then step 8 |
| Every card shows "—" | Part D |
| No Kalshi prices | Part D |
| No emails arriving | Steps 15 and 16 |
