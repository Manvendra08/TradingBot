import re

with open('scratch/tv_page.html', 'r', encoding='utf-8') as f:
    html = f.read()

# Let's search for "manav_08" or "manvendra"
username_matches = []
for m in re.finditer(r'manav_08', html, re.IGNORECASE):
    username_matches.append(html[max(0, m.start()-150):min(len(html), m.end()+150)])

print(f"Found {len(username_matches)} occurrences of user identifier:")
for m in username_matches:
    print("---")
    print(m)
