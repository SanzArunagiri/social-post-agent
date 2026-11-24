"""The only HTML this server has: the OAuth result page and the one-time upload page."""
from __future__ import annotations

import html
import json

STYLE = """<style>
body{margin:0;min-height:100vh;display:grid;place-items:center;background:#0f1115;color:#e6e8eb;
font:16px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
.box{width:min(520px,92vw);text-align:center}h1{font-size:22px;margin:0 0 8px}p{color:#aab0b8}
.ok{color:#5ee49a}.bad{color:#ff7b7b}
label{display:block;border:2px dashed #5b8cff;border-radius:12px;padding:40px 16px;cursor:pointer;margin-top:16px}
label.over{background:#1a2133}#out{text-align:left;margin-top:16px;font-size:14px}#out div{padding:2px 0}
code{background:#1b1e24;padding:1px 5px;border-radius:4px}
</style>"""


def result_page(title: str, message: str, ok: bool, items: list[str] | None = None) -> str:
    lis = "".join(f"<div>{html.escape(i)}</div>" for i in items or [])
    return (f"<!DOCTYPE html><meta name=viewport content='width=device-width,initial-scale=1'><title>{html.escape(title)}</title>"
            f"{STYLE}<div class=box><h1 class={'ok' if ok else 'bad'}>{html.escape(title)}</h1>"
            f"<p>{html.escape(message)}</p><div>{lis}</div></div>")


def upload_page(note: str, max_files: int) -> str:
    # JSON inside <script>: escape <, > and & so a note can never close the script tag
    note_js = json.dumps(note or "Images and videos for your post").replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    return """<!DOCTYPE html><meta name=viewport content='width=device-width,initial-scale=1'>
<meta name=referrer content=no-referrer><title>Upload media</title>""" + STYLE + """
<div class=box><h1>Upload media</h1><p id=note></p>
<label id=drop>Tap to choose images / videos, or drop them here
<input id=f type=file multiple accept="image/*,video/*" hidden></label>
<div id=out></div><p id=done class=ok hidden>Done - you can close this page and tell your assistant the files are uploaded.</p></div>
<script>
const MAX=""" + str(int(max_files)) + """, out=document.getElementById("out"), drop=document.getElementById("drop");
document.getElementById("note").textContent=""" + note_js + """;
async function send(files){
  for(const file of files){
    const row=document.createElement("div"); row.textContent="\\u2b06 "+file.name+" \\u2026"; out.appendChild(row);
    const fd=new FormData(); fd.append("file",file);
    try{
      const r=await fetch(location.pathname,{method:"POST",body:fd});
      const j=await r.json().catch(()=>({}));
      if(!r.ok) throw new Error(j.error||j.detail||("HTTP "+r.status));
      row.textContent="\\u2705 "+file.name+" ("+j.kind+", id "+j.id+")";
      document.getElementById("done").hidden=false;
    }catch(e){ row.textContent="\\u274c "+file.name+": "+e.message; }
  }
}
document.getElementById("f").onchange=e=>send(e.target.files);
drop.ondragover=e=>{e.preventDefault();drop.classList.add("over")};
drop.ondragleave=()=>drop.classList.remove("over");
drop.ondrop=e=>{e.preventDefault();drop.classList.remove("over");send(e.dataTransfer.files)};
</script>"""
