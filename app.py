import streamlit as st
import streamlit.components.v1 as components
import os
import time
import zipfile
import subprocess
from pathlib import Path

from core.utils import is_valid_url, parse_urls, get_platform, get_video_info
from core.queue_manager import (
    init_queue, add_to_queue, get_queue,
    update_status, clear_completed_from_queue, get_history
)
from core.processor import process_item

# ─── Page Config ───────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="YT Downloader",
    page_icon="🎬",
    layout="centered",
    initial_sidebar_state="collapsed"
)

# ─── Inject CSS ────────────────────────────────────────────────────────────────
css_path = Path(__file__).parent / "static" / "style.css"
if css_path.exists():
    with open(css_path) as f:
        st.markdown(f"<style>{f.read()}</style>", unsafe_allow_html=True)

# ─── Session State Init ─────────────────────────────────────────────────────────
init_queue()
for key, default in [
    ("active_tab",       "download"),
    ("partial_files",    []),
    ("partial_failed",   []),
    ("partial_mode",     "zip"),
    ("show_partial_dlg", False),
]:
    if key not in st.session_state:
        st.session_state[key] = default

# ─── Helpers ────────────────────────────────────────────────────────────────────
def _build_zip(files: list[Path], download_dir: Path) -> Path:
    ts = int(time.time())
    zip_path = download_dir / f"downloads_{ts}.zip"
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_STORED) as zf:
        for fp in files:
            zf.write(fp, Path(fp).name)
    return zip_path


def _build_joined(files: list[Path], download_dir: Path):
    ts = int(time.time())
    list_path = download_dir / f"concat_{ts}.txt"
    with open(list_path, 'w') as lf:
        for fp in files:
            lf.write(f"file '{Path(fp).name}'\n")
    ext = Path(files[0]).suffix
    out_path = download_dir / f"joined_{ts}{ext}"
    result = subprocess.run(
        ['ffmpeg', '-f', 'concat', '-safe', '0', '-i', str(list_path), '-c', 'copy', str(out_path)],
        capture_output=True
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.decode())
    return out_path


def _auto_click_download(filename: str):
    # Inject directly into the page DOM — no iframe needed
    # We use a timestamp to ensure the script tag is fresh and React re-evaluates it
    safe = filename.replace("'", "\\'")
    ts = int(time.time() * 1000)
    st.markdown(
        f"""<script id="auto-click-{ts}">
        setTimeout(() => {{
            document.querySelectorAll('button').forEach(b => {{
                if (b.innerText.includes('{safe}')) {{
                    b.click();
                    b.innerText = "Downloading...";
                    b.disabled = true;
                }}
            }});
        }}, 600);
        </script>""",
        unsafe_allow_html=True
    )


def _serve_file(final_path: Path, mime: str, label: str, auto: bool = True):
    with open(final_path, "rb") as f:
        st.download_button(label=label, data=f, file_name=final_path.name, mime=mime, key="dl_final")
    if auto:
        _auto_click_download(final_path.name)


# ─── Navigation Bar ─────────────────────────────────────────────────────────────
st.markdown("""
<div class="nav-bar">
  <div class="nav-logo">🎬 <span>YT</span> Downloader</div>
  <div class="nav-links">
    <a href="#download">Download</a>
    <a href="#history">History</a>
    <a href="#about">About</a>
  </div>
</div>
""", unsafe_allow_html=True)

# ─── Hero ───────────────────────────────────────────────────────────────────────
st.markdown("""
<div class="hero-title">Download from <span class="accent">YouTube, Instagram & Facebook</span></div>
<div class="hero-sub">Paste one or multiple links — we'll handle the rest ✨</div>
""", unsafe_allow_html=True)

# ─── Supported platforms badge row ─────────────────────────────────────────────
st.markdown("""
<div style="display:flex;gap:0.5rem;justify-content:center;flex-wrap:wrap;margin-bottom:1.5rem;">
  <span style="background:rgba(255,0,0,0.12);color:#ff6b6b;border-radius:999px;padding:0.25rem 0.8rem;font-size:0.78rem;font-weight:600;">▶ YouTube</span>
  <span style="background:rgba(255,0,0,0.12);color:#ff6b6b;border-radius:999px;padding:0.25rem 0.8rem;font-size:0.78rem;font-weight:600;">🎵 YouTube Music</span>
  <span style="background:rgba(225,48,108,0.15);color:#e1306c;border-radius:999px;padding:0.25rem 0.8rem;font-size:0.78rem;font-weight:600;">📸 Instagram</span>
  <span style="background:rgba(24,119,242,0.12);color:#4a9eff;border-radius:999px;padding:0.25rem 0.8rem;font-size:0.78rem;font-weight:600;">📘 Facebook</span>
</div>
""", unsafe_allow_html=True)

# ═══════════════════════════════════════════════════════════════════════════════
# STEP 1 — PASTE LINKS
# ═══════════════════════════════════════════════════════════════════════════════
st.markdown('<div class="step-label">Step 1 — Paste your links</div>', unsafe_allow_html=True)
urls_raw = st.text_area(
    label="Paste video links here",
    placeholder=(
        "https://www.youtube.com/watch?v=...\n"
        "https://music.youtube.com/watch?v=...\n"
        "https://www.instagram.com/p/...\n"
        "https://www.facebook.com/watch?v=...\n\n"
        "One link per line — mix platforms freely!"
    ),
    height=130,
    label_visibility="collapsed"
)

# ── Parse URLs robustly ─────────────────────────────────────────────────────────
valid_urls, invalid_raws = parse_urls(urls_raw)

# Detect duplicate valid URLs (after de-duplication, these are already gone — show a notice)
# parse_urls already de-dupes so we just count originals vs parsed
raw_count = len([l.strip() for l in urls_raw.split('\n') if l.strip()])
deduped_notice = raw_count > len(valid_urls) + len(invalid_raws)

# ═══════════════════════════════════════════════════════════════════════════════
# STEP 2 — CHOOSE FORMAT & QUALITY
# ═══════════════════════════════════════════════════════════════════════════════
st.markdown('<div class="step-label">Step 2 — Choose what you want</div>', unsafe_allow_html=True)

col_mode, col_quality = st.columns(2)
with col_mode:
    st.markdown('<div class="card-title">🎞 Format</div>', unsafe_allow_html=True)
    mode = st.radio("Format", ["🎬  Video", "🎵  Audio"], horizontal=True, label_visibility="collapsed")
    mode_clean = "Video" if "Video" in mode else "Audio"

with col_quality:
    st.markdown('<div class="card-title">✨ Quality</div>', unsafe_allow_html=True)
    if mode_clean == "Video":
        quality = st.radio("Quality", ["Default", "1080p", "720p", "480p", "360p"], horizontal=True, label_visibility="collapsed")
    else:
        quality = st.radio("Quality", ["Default", "320kbps", "192kbps", "128kbps"], horizontal=True, label_visibility="collapsed")

# ═══════════════════════════════════════════════════════════════════════════════
# STEP 3 — OUTPUT FORMAT (only for multiple URLs)
# ═══════════════════════════════════════════════════════════════════════════════
if len(valid_urls) > 1:
    st.markdown('<div class="step-label">Step 3 — How to receive your files?</div>', unsafe_allow_html=True)
    output_format = st.radio(
        "Output",
        ["📦  Separate files (ZIP)", "🔗  Join into one file"],
        horizontal=True,
        label_visibility="collapsed"
    )
    output_clean = "zip" if "ZIP" in output_format else "joined"
else:
    output_clean = "zip"

# ─── Live validation feedback ────────────────────────────────────────────────
if invalid_raws:
    with st.expander(f"⚠️ {len(invalid_raws)} unsupported link(s) found — click to see", expanded=False):
        st.markdown(
            "Only **YouTube**, **YouTube Music**, **Instagram** and **Facebook** links are supported. "
            "The following will be skipped:"
        )
        for bad in invalid_raws:
            st.code(bad, language=None)

if deduped_notice:
    st.info("ℹ️ Duplicate links were automatically removed.")

if valid_urls:
    platform_labels = ", ".join(sorted(set(get_platform(u) for u in valid_urls)))
    st.success(f"✅ {len(valid_urls)} valid link{'s' if len(valid_urls) > 1 else ''} ready  ·  {platform_labels}")

# ═══════════════════════════════════════════════════════════════════════════════
# DOWNLOAD BUTTON
# ═══════════════════════════════════════════════════════════════════════════════
st.markdown("<br>", unsafe_allow_html=True)
start_download = st.button("⬇️  Download Now", type="primary", disabled=len(valid_urls) == 0)

# ═══════════════════════════════════════════════════════════════════════════════
# PARTIAL FAILURE MODAL (Join mode had some failures)
# Injected into the PARENT page DOM via JS so it truly covers everything
# ═══════════════════════════════════════════════════════════════════════════════
if st.session_state.show_partial_dlg:
    pf     = st.session_state.partial_failed
    pfiles = st.session_state.partial_files
    download_dir = Path(__file__).parent / "downloads"

    # Build escaped JS strings for each failed item
    failed_rows_js = ""
    for f in pf:
        url_esc = f["url"][:65].replace("'", "\\'").replace("`", "\\`")
        err_esc = f["error"][:100].replace("'", "\\'").replace("`", "\\`").replace("\n", " ")
        failed_rows_js += f"""
        row = doc.createElement('div'); row.className = 'ytdl-fail-row';
        row.innerHTML = `<span class='ytdl-dot'>●</span><div><div class='ytdl-fail-url'>{url_esc}{'…' if len(f['url']) > 65 else ''}</div><div class='ytdl-fail-err'>{err_esc}</div></div>`;
        failBox.appendChild(row);
        """

    n_fail  = len(pf)
    n_ok    = len(pfiles)
    plural  = "s" if n_fail > 1 else ""
    ok_plural = "s" if n_ok > 1 else ""

    # Inject modal directly into the page DOM via a <script> tag
    st.markdown(f"""
    <script>
    (function() {{
      const doc = window.parent.document;

      // Remove any existing modal
      const old = doc.getElementById('ytdl-modal-backdrop');
      if (old) old.remove();

      // ── Inject CSS into parent <head> ──────────────────────────────────────
      if (!doc.getElementById('ytdl-modal-styles')) {{
        const style = doc.createElement('style');
        style.id = 'ytdl-modal-styles';
        style.textContent = `
          #ytdl-modal-backdrop {{
            position: fixed !important;
            inset: 0 !important;
            background: rgba(0,0,0,0.78) !important;
            backdrop-filter: blur(8px) !important;
            -webkit-backdrop-filter: blur(8px) !important;
            z-index: 999999 !important;
            display: flex !important;
            align-items: center !important;
            justify-content: center !important;
            animation: ytdlFadeIn .2s ease !important;
          }}
          @keyframes ytdlFadeIn {{ from {{ opacity:0 }} to {{ opacity:1 }} }}

          #ytdl-modal-card {{
            background: linear-gradient(145deg,#1e1535,#16102b);
            border: 1px solid rgba(168,85,247,0.4);
            border-radius: 20px;
            padding: 2rem;
            width: min(520px, 90vw);
            box-shadow: 0 40px 100px rgba(0,0,0,0.7), 0 0 0 1px rgba(255,255,255,0.04);
            font-family: 'Inter', system-ui, sans-serif;
            animation: ytdlSlideUp .28s cubic-bezier(.22,.68,0,1.2);
            pointer-events: all;
          }}
          @keyframes ytdlSlideUp {{
            from {{ transform: translateY(28px); opacity:0 }}
            to   {{ transform: translateY(0);    opacity:1 }}
          }}

          .ytdl-modal-icon {{
            width:44px; height:44px;
            background:rgba(251,191,36,0.12);
            border-radius:12px;
            display:flex; align-items:center; justify-content:center;
            font-size:1.4rem; margin-bottom:.9rem;
          }}
          .ytdl-modal-title {{
            font-size:1.1rem; font-weight:700; color:#fff; margin-bottom:.35rem;
          }}
          .ytdl-modal-sub {{
            font-size:.875rem; color:rgba(255,255,255,.5);
            line-height:1.55; margin-bottom:1.1rem;
          }}
          .ytdl-modal-sub strong {{ color:#fbbf24; }}
          .ytdl-fail-box {{
            background:rgba(255,255,255,0.04);
            border:1px solid rgba(255,255,255,0.07);
            border-radius:10px;
            padding:.65rem .9rem;
            margin-bottom:1.3rem;
            max-height:150px; overflow-y:auto;
          }}
          .ytdl-fail-row {{
            display:flex; gap:.55rem; align-items:flex-start;
            padding:.3rem 0;
            border-bottom:1px solid rgba(255,255,255,0.05);
          }}
          .ytdl-fail-row:last-child {{ border-bottom:none; }}
          .ytdl-dot {{ color:#ef4444; font-size:.55rem; margin-top:.38rem; flex-shrink:0; }}
          .ytdl-fail-url {{ font-size:.78rem; color:rgba(255,255,255,.8); word-break:break-all; }}
          .ytdl-fail-err  {{ font-size:.7rem;  color:rgba(255,255,255,.35); margin-top:.1rem; }}
          .ytdl-actions {{ display:flex; flex-direction:column; gap:.55rem; }}
          .ytdl-btn {{
            width:100%; padding:.68rem 1rem;
            border-radius:11px; border:none;
            font-size:.88rem; font-weight:600;
            cursor:pointer; transition:all .17s;
            display:flex; align-items:center; gap:.55rem;
            font-family:inherit;
          }}
          .ytdl-btn-join {{
            background:linear-gradient(135deg,#a855f7,#7c3aed); color:#fff;
          }}
          .ytdl-btn-join:hover {{ opacity:.85; transform:translateY(-1px); }}
          .ytdl-btn-zip {{
            background:rgba(255,255,255,0.07);
            border:1px solid rgba(255,255,255,0.13) !important;
            color:rgba(255,255,255,.82);
          }}
          .ytdl-btn-zip:hover {{ background:rgba(255,255,255,0.12); }}
          .ytdl-btn-cancel {{
            background:transparent;
            border:1px solid rgba(255,255,255,0.08) !important;
            color:rgba(255,255,255,.32);
            font-size:.8rem;
            justify-content:center;
          }}
          .ytdl-btn-cancel:hover {{ color:rgba(255,255,255,.6); border-color:rgba(255,255,255,0.2) !important; }}
        `;
        doc.head.appendChild(style);
      }}

      // ── Build the modal DOM ────────────────────────────────────────────────
      const backdrop = doc.createElement('div');
      backdrop.id = 'ytdl-modal-backdrop';

      const card = doc.createElement('div');
      card.id = 'ytdl-modal-card';

      // Header
      card.innerHTML = `
        <div class="ytdl-modal-icon">⚠️</div>
        <div class="ytdl-modal-title">{n_fail} link{plural} failed to download</div>
        <div class="ytdl-modal-sub">
          Only <strong>{n_ok} file{ok_plural}</strong> downloaded successfully.
          The failed ones are listed below — choose how to proceed.
        </div>
      `;

      // Failed links list
      const failBox = doc.createElement('div');
      failBox.className = 'ytdl-fail-box';
      let row;
      {failed_rows_js}
      card.appendChild(failBox);

      // Action buttons
      const actions = doc.createElement('div');
      actions.className = 'ytdl-actions';

      function makeBtn(cls, icon, txt, key) {{
        const b = doc.createElement('button');
        b.className = 'ytdl-btn ' + cls;
        b.innerHTML = icon + '&nbsp;&nbsp;' + txt;
        b.onclick = () => triggerHiddenBtn(key);
        return b;
      }}

      actions.appendChild(makeBtn('ytdl-btn-join',   '🔗', 'Join available files into one',  'dlg_join'));
      actions.appendChild(makeBtn('ytdl-btn-zip',    '📦', 'Download as ZIP instead',        'dlg_zip'));
      actions.appendChild(makeBtn('ytdl-btn-cancel', '✕',  'Cancel',                         'dlg_cancel'));
      card.appendChild(actions);

      backdrop.appendChild(card);
      doc.body.appendChild(backdrop);

      // Lock page scroll while modal is open
      doc.body.style.overflow = 'hidden';

      // ── Wire buttons to hidden Streamlit buttons ───────────────────────────
      function triggerHiddenBtn(key) {{
        const allBtns = doc.querySelectorAll('button');
        allBtns.forEach(b => {{
          const txt = (b.innerText || '').trim();
          if (key === 'dlg_join'   && txt === 'Join available files')   {{ b.click(); cleanup(); }}
          if (key === 'dlg_zip'    && txt === 'Download as ZIP instead') {{ b.click(); cleanup(); }}
          if (key === 'dlg_cancel' && txt === 'Cancel')                  {{ b.click(); cleanup(); }}
        }});
      }}

      function cleanup() {{
        const el = doc.getElementById('ytdl-modal-backdrop');
        if (el) el.remove();
        doc.body.style.overflow = '';
      }}

      // Close on backdrop click (outside card)
      backdrop.addEventListener('click', e => {{
        if (e.target === backdrop) {{
          // treat as cancel
          triggerHiddenBtn('dlg_cancel');
        }}
      }});

      // ESC key to cancel
      doc.addEventListener('keydown', function escHandler(e) {{
        if (e.key === 'Escape') {{
          triggerHiddenBtn('dlg_cancel');
          doc.removeEventListener('keydown', escHandler);
        }}
      }});

    }})();
    </script>
    """, unsafe_allow_html=True)

    # Hidden Streamlit buttons — invisible, but the modal JS clicks them
    st.markdown("""
    <style>
      div[data-testid="stHorizontalBlock"]:has(#dlg_join_wrapper) { display: none !important; }
    </style>
    """, unsafe_allow_html=True)

    with st.container():
        c1, c2, c3 = st.columns(3)
        with c1:
            if st.button("Join available files", key="dlg_join"):
                with st.spinner("🔗 Joining available files... This may take a minute."):
                    try:
                        joined = _build_joined(pfiles, download_dir)
                        st.session_state.show_partial_dlg = False
                        _serve_file(joined, "application/octet-stream", f"⬇️  Save joined — {joined.name}")
                    except Exception as e:
                        st.error(f"Join failed: {e}")
        with c2:
            if st.button("Download as ZIP instead", key="dlg_zip"):
                with st.spinner("📦 Zipping available files..."):
                    zp = _build_zip(pfiles, download_dir)
                    st.session_state.show_partial_dlg = False
                    _serve_file(zp, "application/zip", f"⬇️  Save ZIP — {zp.name}")
        with c3:
            if st.button("Cancel", key="dlg_cancel"):
                st.session_state.show_partial_dlg = False
                st.session_state.partial_files  = []
                st.session_state.partial_failed = []
                st.rerun()


# ═══════════════════════════════════════════════════════════════════════════════
# PROCESSING
# ═══════════════════════════════════════════════════════════════════════════════
if start_download and valid_urls:
    clear_completed_from_queue()

    item_ids = []
    for url in valid_urls:
        item_id = add_to_queue(url, mode_clean, quality)
        item_ids.append(item_id)

    queue = get_queue()
    pending_items = [i for i in queue if i['id'] in item_ids]

    st.markdown("---")
    st.markdown('<div class="card-title">⚡ Processing your downloads…</div>', unsafe_allow_html=True)

    processed_files = []
    failed_items    = []

    for item in pending_items:
        short_url = (item['url'][:62] + '…') if len(item['url']) > 62 else item['url']
        platform  = get_platform(item['url'])

        # ── Per-item header ─────────────────────────────────────────────────────
        st.markdown(
            f'<div class="status-item">'
            f'<div class="status-info">'
            f'<div class="status-title">🔗 {short_url}</div>'
            f'<div class="status-meta">{platform} · {mode_clean} · {quality}</div>'
            f'</div></div>',
            unsafe_allow_html=True
        )

        # placeholders updated in real-time
        progress_ph  = st.empty()   # progress bar
        detail_ph    = st.empty()   # speed / ETA / size row
        status_ph    = st.empty()   # badge
        warn_ph      = st.empty()   # warning messages

        progress_ph.progress(0)
        status_ph.markdown(
            '<span class="badge badge-processing">⏳ Starting…</span>',
            unsafe_allow_html=True
        )

        last_pct = 0

        for update in process_item(item):
            s = update.get("status", "")

            # ── Real progress update ─────────────────────────────────────────
            if s == "Progress":
                pct   = update.get("percent", last_pct)
                speed = update.get("speed", "")
                eta   = update.get("eta", "")
                dl    = update.get("downloaded", "")
                tot   = update.get("total", "")
                last_pct = pct

                progress_ph.progress(pct)

                # Rich detail row
                parts = []
                if dl and tot and tot != "?":
                    parts.append(f"📦 {dl} / {tot}")
                elif dl:
                    parts.append(f"📦 {dl}")
                if speed:
                    parts.append(f"⚡ {speed}")
                if eta:
                    parts.append(f"⏱ ETA {eta}")

                detail_text = "  ·  ".join(parts) if parts else "Downloading…"
                detail_ph.markdown(
                    f'<div style="font-size:.78rem;color:rgba(255,255,255,.45);'
                    f'padding:.1rem 0 .4rem;">{detail_text}</div>',
                    unsafe_allow_html=True
                )
                status_ph.markdown(
                    f'<span class="badge badge-processing">⬇ {pct}%</span>',
                    unsafe_allow_html=True
                )

            # ── Merging (ffmpeg post-process) ────────────────────────────────
            elif s == "Merging":
                progress_ph.progress(97)
                detail_ph.markdown(
                    '<div style="font-size:.78rem;color:rgba(255,255,255,.45);'
                    'padding:.1rem 0 .4rem;">🔧 Merging audio + video tracks…</div>',
                    unsafe_allow_html=True
                )
                status_ph.markdown(
                    '<span class="badge badge-processing">🔧 Merging…</span>',
                    unsafe_allow_html=True
                )

            # ── Slow connection warning ──────────────────────────────────────
            elif s == "Warning":
                warn_ph.markdown(
                    f'<div style="font-size:.78rem;color:#fbbf24;'
                    f'padding:.2rem 0;">⚠️ {update["message"]}</div>',
                    unsafe_allow_html=True
                )

            # ── Completed ────────────────────────────────────────────────────
            elif s == "Completed":
                progress_ph.progress(100)
                sz = update.get("downloaded", "")
                detail_ph.markdown(
                    f'<div style="font-size:.78rem;color:rgba(34,197,94,.7);'
                    f'padding:.1rem 0 .4rem;">✓ Saved{(" · " + sz) if sz else ""}</div>',
                    unsafe_allow_html=True
                )
                warn_ph.empty()
                status_ph.markdown(
                    '<span class="badge badge-done">✓ Done</span>',
                    unsafe_allow_html=True
                )
                update_status(item['id'], 'Completed')
                fp = update.get('file_path')
                if fp and os.path.exists(fp):
                    processed_files.append(Path(fp))
                else:
                    update_status(item['id'], 'Failed', 'File not found after download')
                    failed_items.append({'url': item['url'], 'error': 'File not found after download'})
                    status_ph.markdown(
                        '<span class="badge badge-failed">✗ File missing</span>',
                        unsafe_allow_html=True
                    )

            # ── Failed ───────────────────────────────────────────────────────
            elif s == "Failed":
                progress_ph.empty()
                detail_ph.empty()
                warn_ph.empty()
                err_msg = update.get('error', update.get('message', 'Unknown error'))
                status_ph.markdown(
                    '<span class="badge badge-failed">✗ Failed</span>',
                    unsafe_allow_html=True
                )
                st.markdown(
                    f'<div style="background:rgba(239,68,68,0.08);border:1px solid rgba(239,68,68,0.2);'
                    f'border-radius:8px;padding:.6rem .9rem;font-size:.82rem;color:#fca5a5;margin:.3rem 0;">'
                    f'❌ {err_msg}</div>',
                    unsafe_allow_html=True
                )
                update_status(item['id'], 'Failed', err_msg)
                failed_items.append({'url': item['url'], 'error': err_msg})

        # Small visual separator between items
        st.markdown('<div style="margin-bottom:.5rem;"></div>', unsafe_allow_html=True)



    # ── Post-processing: build output file ─────────────────────────────────────
    st.markdown("---")
    download_dir = Path(__file__).parent / "downloads"
    ts = int(time.time())

    total   = len(pending_items)
    n_ok    = len(processed_files)
    n_fail  = len(failed_items)

    if n_ok == 0:
        st.error("😕 All downloads failed. Please check your links and try again.")

    else:
        # Show partial failure summary (ZIP mode — non-blocking)
        if n_fail > 0 and output_clean == "zip":
            st.warning(
                f"⚠️ **{n_fail} of {total} link(s) failed.** "
                f"The ZIP will contain only the **{n_ok}** successful download(s)."
            )
            with st.expander("See failed links"):
                for fail in failed_items:
                    st.markdown(f"🔴 `{fail['url']}`  \n*{fail['error']}*")

        # JOIN mode with partial failures → show blocking dialog
        if output_clean == "joined" and n_fail > 0:
            st.session_state.partial_files  = processed_files
            st.session_state.partial_failed = failed_items
            st.session_state.partial_mode   = output_clean
            st.session_state.show_partial_dlg = True
            st.rerun()

        # Join — all succeeded, or ZIP mode
        elif output_clean == "joined" and n_ok > 1:
            with st.spinner("🔗 Joining files together... This may take a minute."):
                try:
                    final_path = _build_joined(processed_files, download_dir)
                    mime       = "application/octet-stream"
                    label      = f"⬇️  Save joined file — {final_path.name}"
                except Exception as e:
                    st.error(f"Could not join files: {e}. Falling back to ZIP.")
                    final_path = _build_zip(processed_files, download_dir)
                    mime       = "application/zip"
                    label      = f"⬇️  Save ZIP — {final_path.name}"

            st.success(f"🎉 {n_ok} file(s) joined and ready!")
            _serve_file(final_path, mime, label)

        elif output_clean == "joined" and n_ok == 1:
            # Only 1 file — joining makes no sense
            st.info("ℹ️ Only 1 file was downloaded, so joining is not needed — downloading directly.")
            final_path = processed_files[0]
            mime       = "application/octet-stream"
            label      = f"⬇️  Save — {final_path.name}"
            _serve_file(final_path, mime, label)

        else:
            # ZIP mode
            with st.spinner("📦 Zipping files..."):
                final_path = _build_zip(processed_files, download_dir)
            if n_ok == n_fail == 0:
                pass
            elif n_ok == 1:
                # Only 1 file — skip zip wrapping, serve directly
                final_path = processed_files[0]
                mime       = "application/octet-stream"
                label      = f"⬇️  Save — {final_path.name}"
            else:
                mime  = "application/zip"
                label = f"⬇️  Save all {n_ok} files — {final_path.name}"

            st.success(f"🎉 {n_ok} file{'s' if n_ok > 1 else ''} ready!")
            _serve_file(final_path, mime, label)

    clear_completed_from_queue()

# ═══════════════════════════════════════════════════════════════════════════════
# HISTORY SECTION
# ═══════════════════════════════════════════════════════════════════════════════
history = get_history()
if history:
    st.markdown("---")
    st.markdown('<a name="history"></a>', unsafe_allow_html=True)
    col_ht, col_hb = st.columns([4, 1])
    with col_ht:
        st.markdown('<div class="card-title">🕘 Recently Downloaded</div>', unsafe_allow_html=True)
    with col_hb:
        if st.button("Clear History", type="secondary"):
            st.session_state.history = []
            st.rerun()

    for h in reversed(history[-12:]):
        icon      = "🟢" if h['status'] == 'Completed' else "🔴"
        short_url = (h['url'][:58] + '…') if len(h['url']) > 58 else h['url']
        platform  = get_platform(h['url'])
        err_snip  = f' — {h["error"][:70]}' if h.get("error") else ''
        st.markdown(
            f'<div class="history-item">'
            f'<div style="font-size:1.3rem;">{icon}</div>'
            f'<div class="history-info">'
            f'<div class="history-title">{short_url}</div>'
            f'<div class="history-meta">{platform} · {h["mode"]} · {h["quality"]} · {h["status"]}{err_snip}</div>'
            f'</div></div>',
            unsafe_allow_html=True
        )

# ═══════════════════════════════════════════════════════════════════════════════
# ABOUT SECTION
# ═══════════════════════════════════════════════════════════════════════════════
st.markdown("---")
st.markdown('<a name="about"></a>', unsafe_allow_html=True)
with st.expander("ℹ️ About this app"):
    st.markdown("""
**YT Downloader** supports:

| Platform | What you can download |
|---|---|
| ▶ YouTube | Videos, Shorts, Playlists |
| 🎵 YouTube Music | Songs, Albums |
| 📸 Instagram | Reels, Posts, Stories |
| 📘 Facebook | Public videos, Reels |

> ⚠️ **Please only download content you have permission to access.**
> This app is intended as a personal, educational tool.
    """)
