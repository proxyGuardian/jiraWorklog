import requests
from datetime import datetime, timedelta
from collections import defaultdict
import calendar

# === CONFIG ===
JIRA_URL = "https://jira.cargo-partner.com"
USERNAME = "pnXXXX"  # Your Jira username
PAT = "XXXX"  # Paste your PAT here

HEADERS = {
    "Authorization": f"Bearer {PAT}",
    "Content-Type": "application/json"
}

# === DATE RANGE FOR THIS MONTH ===
today = datetime.now()
start_date = today.replace(day=1)
end_date = today.replace(day=calendar.monthrange(today.year, today.month)[1])

# === SLOVAK HOLIDAYS (2025) ===
SLOVAK_HOLIDAYS_2025 = {
    "2025-01-01", "2025-01-06", "2025-04-18", "2025-04-21",
    "2025-05-01", "2025-05-08", "2025-07-05", "2025-08-29",
    "2025-09-01", "2025-09-15", "2025-11-01", "2025-11-17",
    "2025-12-24", "2025-12-25", "2025-12-26"
}

def is_workday(date_obj):
    return (
        date_obj.weekday() < 5 and
        date_obj.strftime("%Y-%m-%d") not in SLOVAK_HOLIDAYS_2025
    )

# === FETCH ISSUES ===
def fetch_my_issues(username):
    issues = []
    start_at = 0
    while True:
        jql = f'worklogAuthor = "{username}" AND worklogDate >= startOfMonth()'
        url = f"{JIRA_URL}/rest/api/2/search"
        params = {
            "jql": jql,
            "fields": "summary",
            "startAt": start_at,
            "maxResults": 50
        }
        r = requests.get(url, headers=HEADERS, params=params)
        if r.status_code != 200:
            print(f"❌ Failed to fetch issues: {r.status_code}")
            break
        data = r.json()
        issues += data["issues"]
        if start_at + 50 >= data.get("total", 0):
            break
        start_at += 50
    return issues

def fetch_worklogs(issue_key):
    url = f"{JIRA_URL}/rest/api/2/issue/{issue_key}/worklog"
    r = requests.get(url, headers=HEADERS)
    if r.status_code != 200:
        return []
    return r.json().get("worklogs", [])

# === GATHER WORKLOG DATA ===
def tracked_hours_with_details(username):
    issues = fetch_my_issues(username)
    result = defaultdict(list)

    for issue in issues:
        key = issue["key"]
        summary = issue["fields"]["summary"]
        worklogs = fetch_worklogs(key)

        for wl in worklogs:
            author = wl.get("author", {}).get("name") or wl.get("author", {}).get("accountId")
            if author != username:
                continue
            dt = datetime.strptime(wl["started"][:10], "%Y-%m-%d")
            if not (start_date <= dt <= end_date) or not is_workday(dt):
                continue
            date_str = dt.strftime("%Y-%m-%d")
            hours = wl.get("timeSpentSeconds", 0) / 3600
            result[date_str].append({
                "issue": key,
                "summary": summary,
                "hours": round(hours, 2)
            })
    return result

# === OUTPUT ===
daily_logs = tracked_hours_with_details(USERNAME)

print(f"\n🕒 Tracked Worklogs (Workdays only) – {today.strftime('%B %Y')}")
print("Date       | Hours | Task ID    | Summary")
print("-----------|-------|------------|--------")

for i in range(1, end_date.day + 1):
    date_obj = start_date.replace(day=i)
    if not is_workday(date_obj):
        continue
    date_str = date_obj.strftime("%Y-%m-%d")
    logs = daily_logs.get(date_str, [])
    total = 0.0
    for log in logs:
        total += log['hours']
        print(f"{date_str} | {log['hours']:>5.2f} | {log['issue']:<10} | {log['summary']}")
    if logs:
        print(f"{' ' * 11}Total  | {total:>5.2f} h\n")
