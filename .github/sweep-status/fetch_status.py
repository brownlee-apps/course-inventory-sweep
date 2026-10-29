#!/usr/bin/env python3
"""Course status for the Course Inventory Sweep, read from the Jotform submission form.

Run by .github/workflows/sweep-status.yml every 15 minutes. It reads every submission on the Course Inventory Sweep
Submission form through Jotform's API, keeps the newest live submission for each Sweep course, and writes
live_status.json beside index.html, which the Sweep reads when it opens.

The workflow in Jotform writes where each submission is waiting into the hidden Status question (Unique Name status)
with Update Value elements: Dean (the default), Banner (a Dean approved it), Degree Works, Catalog, Complete, and
Returned (a Dean denied it). Anything else, a blank included, counts as received and in process.

What leaves Jotform: only each course's record ID, the date its newest report was submitted, and its Status.
The repository and its logs are public, so the script prints counts only. It skips Workflow Test Runs, anything not
among the Sweep's courses (courses.txt), and every submission that is not active.

Usage:
  JOTFORM_API_KEY=... python3 fetch_status.py --courses courses.txt --out live_status.json [--dry-run]
  python3 fetch_status.py --courses courses.txt --out live_status.json --from-json SUBMISSIONS.json   (tests)

--dry-run includes Test Runs, prints the counts, and writes nothing. It shows the key and the parsing work before any
live report exists. The script writes the file only when some course's status or date changed, and then sets
changed=true in $GITHUB_OUTPUT so the workflow commits it.
"""
import argparse, datetime, json, os, re, sys, urllib.error, urllib.parse, urllib.request
from collections import Counter

FORM_ID = os.environ.get("JOTFORM_FORM_ID", "262822849097066")
API_BASE = os.environ.get("JOTFORM_API_BASE", "https://bpcc.jotform.com/API").rstrip("/")
PAGE = 1000
# Question IDs on the form, with the Unique Names as a fallback (Course q2 textbox0, PDF File Name q9 textbox7, Status q25 status)
COURSE_Q, PDF_Q, STATUS_Q = "2", "9", "25"
COURSE_NAMES, PDF_NAMES, STATUS_NAMES = {"textbox0", "q2_textbox0"}, {"textbox7", "q9_textbox7"}, {"status", "q25_status"}
# Status values the workflow writes, matched forgivingly (letters only, any case)
STATUS_MAP = {
    "dean": "dean", "deanreview": "dean",
    "banner": "banner",
    "degreeworks": "degreeworks", "degwks": "degreeworks",
    "catalog": "catalog", "publiccatalog": "catalog",
    "complete": "complete", "completed": "complete",
    "returned": "returned",
}
STATUSES = ["dean", "banner", "degreeworks", "catalog", "complete", "returned", "received"]
# Refuse to write when this many courses vanish at once, since live submissions are not deleted in normal use
MAX_DROP = 10


def fail(msg):
    print(f"Stopped: {msg}", file=sys.stderr)
    sys.exit(1)


def fetch_all(key):
    subs, offset = [], 0
    while True:
        q = urllib.parse.urlencode({"limit": PAGE, "offset": offset, "orderby": "created_at"})
        req = urllib.request.Request(f"{API_BASE}/form/{FORM_ID}/submissions?{q}",
                                     headers={"APIKEY": key, "User-Agent": "BPCC-Course-Sweep-Status/1.0", "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                body = json.load(r)
        except urllib.error.HTTPError as e:
            fail(f"Jotform answered HTTP {e.code}. Check that JOTFORM_API_KEY is a current key with read access.")
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            fail(f"Could not read from Jotform ({type(e).__name__}).")
        if body.get("responseCode") != 200 or not isinstance(body.get("content"), list):
            fail(f"Jotform answered responseCode {body.get('responseCode')}.")
        page = body["content"]
        subs.extend(page)
        if len(page) < PAGE:
            return subs
        offset += PAGE


def answer(sub, qid, names):
    answers = sub.get("answers") or {}
    a = answers.get(qid)
    if not a or (a.get("name") not in names and names):
        a = next((v for v in answers.values() if v.get("name") in names), a)
    v = (a or {}).get("answer")
    return v.strip() if isinstance(v, str) else ""


def is_test_run(sub):
    if str(sub.get("workflowTestRun", "")) in ("1", "true"):
        return True
    for a in (sub.get("answers") or {}).values():
        v = a.get("answer")
        if a.get("type") == "control_submission_info" and isinstance(v, dict) and str(v.get("workflowTestRun", "")) in ("1", "true"):
            return True
    return False


def course_id(sub, known):
    cid = re.sub(r"\s+", "", answer(sub, COURSE_Q, COURSE_NAMES)).upper()
    if cid in known:
        return cid
    # fall back to the submission ID in the PDF file name (MATH102_Updated_20261029_101530.pdf)
    pdf = answer(sub, PDF_Q, PDF_NAMES).upper()
    cid = re.sub(r"\s+", "", pdf.split("_")[0])
    return cid if cid in known else None


def norm_status(v):
    return STATUS_MAP.get(re.sub(r"[^a-z]", "", (v or "").lower()), "received")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--courses", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--dry-run", action="store_true", help="include Test Runs, print counts, write nothing")
    ap.add_argument("--from-json", help="read submissions from a file instead of Jotform (tests)")
    args = ap.parse_args()

    known = {line.strip() for line in open(args.courses, encoding="utf-8") if line.strip()}
    if len(known) < 100:
        fail(f"{args.courses} lists only {len(known)} courses.")
    if args.from_json:
        subs = json.load(open(args.from_json, encoding="utf-8"))
    else:
        key = os.environ.get("JOTFORM_API_KEY", "").strip()
        if not key:
            fail("JOTFORM_API_KEY is not set. Add it as a repository secret.")
        subs = fetch_all(key)

    seen = Counter()
    newest = {}
    for s in subs:
        seen["read"] += 1
        if str(s.get("status", "ACTIVE")).upper() != "ACTIVE":
            seen["not active"] += 1
            continue
        if is_test_run(s):
            seen["test runs"] += 1
            if not args.dry_run:
                continue
        cid = course_id(s, known)
        if not cid:
            seen["not a Sweep course"] += 1
            continue
        when = str(s.get("created_at", ""))
        rank = (when, str(s.get("id", "")))
        if cid not in newest or rank > newest[cid][0]:
            newest[cid] = (rank, {"status": norm_status(answer(s, STATUS_Q, STATUS_NAMES)), "submitted": when[:10]})
    courses = {cid: newest[cid][1] for cid in sorted(newest)}
    by_status = Counter(v["status"] for v in courses.values())

    print(f"Submissions read: {seen['read']}. Skipped: {seen['test runs']} Test Run(s){' (counted in this dry run)' if args.dry_run else ''}, "
          f"{seen['not a Sweep course']} not a Sweep course, {seen['not active']} not active.")
    print(f"Courses with a status: {len(courses)} of {len(known)}. " + ", ".join(f"{s} {by_status[s]}" for s in STATUSES if by_status[s]))

    if args.dry_run:
        print("Dry run: nothing written.")
        return

    old = {}
    if os.path.exists(args.out):
        try:
            old = json.load(open(args.out, encoding="utf-8")).get("courses", {})
        except (json.JSONDecodeError, AttributeError):
            old = {}
    if courses == old and os.path.exists(args.out):
        print("No change since the last run.")
        changed = False
    else:
        gone = len(set(old) - set(courses))
        if gone > MAX_DROP:
            fail(f"{gone} courses would lose their status at once. Nothing written. Run it by hand if this is expected.")
        out = {
            "source": "Jotform, Course Inventory Sweep Submission form",
            "changed": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "courses": courses,
        }
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(out, f, indent=1, sort_keys=False)
            f.write("\n")
        print(f"Wrote {args.out}: {sum(1 for c in courses if old.get(c) != courses[c])} course(s) changed, {gone} removed.")
        changed = True
    gh_out = os.environ.get("GITHUB_OUTPUT")
    if gh_out:
        with open(gh_out, "a", encoding="utf-8") as f:
            f.write(f"changed={'true' if changed else 'false'}\n")


if __name__ == "__main__":
    main()
