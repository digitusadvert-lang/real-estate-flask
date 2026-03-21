import os, re

SNIPPET = '''<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<style>
*,*::before,*::after{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{overflow-x:hidden}
.nav{display:flex;flex-wrap:wrap;gap:4px 0}
.nav a{white-space:nowrap;margin-right:12px}
.tbl-wrap{overflow-x:auto;-webkit-overflow-scrolling:touch;width:100%}
table{min-width:480px}
@media(max-width:640px){
  .form-layout,.grid2,.grid3{grid-template-columns:1fr!important}
  .stats{flex-wrap:wrap}
  .stat-card{min-width:120px}
  .header{flex-direction:column;gap:8px}
  .card,.form-container{padding:12px!important}
  .btn{width:100%!important;margin-bottom:6px!important}
  th,td{padding:8px 10px!important;font-size:12px}
}
@media(max-width:480px){
  body{font-size:13px;margin:8px!important}
  h1{font-size:1.1rem}
}
</style>'''

TEMPLATES_DIR = "templates"  # adjust if needed

for root, dirs, files in os.walk(TEMPLATES_DIR):
    for fname in files:
        if not fname.endswith(".html"):
            continue
        fpath = os.path.join(root, fname)
        with open(fpath, "r", encoding="utf-8") as f:
            content = f.read()
        if "viewport" in content:
            print(f"SKIP (already has viewport): {fpath}")
            continue
        # Inject after <head>
        new_content = re.sub(
            r'(<head(?:\s[^>]*)?>)',
            r'\1\n    ' + SNIPPET.replace('\n', '\n    '),
            content, count=1, flags=re.IGNORECASE
        )
        with open(fpath, "w", encoding="utf-8") as f:
            f.write(new_content)
        print(f"PATCHED: {fpath}")

print("Done.")