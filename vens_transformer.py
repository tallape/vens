#!/usr/bin/env python3
"""
=============================================================================
VENS Transformer - Convert text documents to VENS orthography  v2.0
=============================================================================
Converts words in txt/md/csv/epub files to VENS-A or VENS-B.
Preserves case, HTML/MD formatting. Embedded font for EPUB output.

Usage:  python vens_transformer.py
=============================================================================
"""

import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import os
import re
import csv
import zipfile
import tempfile
import shutil
import datetime
from pathlib import Path
from threading import Thread

# =============================================================================
# CONFIG
# =============================================================================
APP_DIR = Path(__file__).resolve().parent
LOOKUP_CSV = APP_DIR / 'g2p_vents_v2.csv'
FONTS_DIR = APP_DIR / 'fonts'
OUTPUT_DIR = APP_DIR  # output files saved here

# =============================================================================
# 1. Load VENS lookup table
# =============================================================================
def load_lookup():
    """Load g2p_vents_v2.csv into dict: {word_lower: (vens_a, vens_b, vens_c)}"""
    lookup = {}
    if not LOOKUP_CSV.exists():
        print(f'WARNING: {LOOKUP_CSV} not found')
        return lookup
    with open(LOOKUP_CSV, 'r', encoding='utf-8') as f:
        reader = csv.reader(f)
        next(reader, None)
        for row in reader:
            if len(row) >= 4:
                word = row[0].strip().strip('"')
                va = row[1].strip().strip('"')
                vb = row[2].strip().strip('"')
                vc = row[3].strip().strip('"')
                if word:
                    lookup[word.lower()] = (va, vb, vc)
    return lookup

LOOKUP = load_lookup()
print(f'Loaded {len(LOOKUP)} word mappings')

# =============================================================================
# 2. Case-preserving word replacement
# =============================================================================
def apply_case(original, replacement):
    """Make replacement match the case pattern of original."""
    if not replacement:
        return original
    if original.isupper():
        return replacement.upper()
    elif original[0].isupper():
        return replacement[0].upper() + replacement[1:] if len(replacement) > 1 else replacement.upper()
    return replacement.lower()

def transform_text(text, mode='a'):
    """
    Replace words with VENS equivalents. mode: 'a'=vens-a, 'b'=vens-b, 'c'=vens-c.
    Preserves HTML/MD tags via safe-zone approach.
    """
    idx = 0 if mode == 'a' else (1 if mode == 'b' else 2)

    def replace_word(match):
        word = match.group(0)
        key = word.lower()
        if key in LOOKUP:
            repl = LOOKUP[key][idx]
            if repl:
                return apply_case(word, repl)
        return word

    WORD_PATTERN = re.compile(r"[A-Za-z]+(?:'[A-Za-z]+)?(?:-[A-Za-z]+)?")

    SAFE_PATTERNS = [
        (re.compile(r'<[^>]+>'), True),
        (re.compile(r'\[([^\]]*)\]\([^)]+\)'), True),
        (re.compile(r'!\[([^\]]*)\]\([^)]+\)'), True),
        (re.compile(r'```[\s\S]*?```'), True),
        (re.compile(r'`[^`]+`'), True),
        (re.compile(r'https?://\S+'), True),
        (re.compile(r'&[a-zA-Z]+;'), True),
        (re.compile(r'&#\d+;'), True),
    ]

    safe_spans = []
    for pattern, _ in SAFE_PATTERNS:
        for m in pattern.finditer(text):
            safe_spans.append((m.start(), m.end()))

    safe_spans.sort()
    merged = []
    for start, end in safe_spans:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))

    result = []
    pos = 0
    for start, end in merged:
        if pos < start:
            result.append(WORD_PATTERN.sub(replace_word, text[pos:start]))
        result.append(text[start:end])
        pos = end
    if pos < len(text):
        result.append(WORD_PATTERN.sub(replace_word, text[pos:]))
    return ''.join(result)


# =============================================================================
# 3. File format handlers
# =============================================================================
def convert_txt(filepath, mode):
    with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
        return transform_text(f.read(), mode)

def convert_csv(filepath, mode):
    with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
        content = f.read()
    lines = content.split('\n')
    result = []
    for line in lines:
        if not line.strip():
            result.append(line)
            continue
        cells = []
        current = ''
        in_quotes = False
        for ch in line:
            if ch == '"':
                in_quotes = not in_quotes
                current += ch
            elif ch == ',' and not in_quotes:
                cells.append(current)
                current = ''
            else:
                current += ch
        cells.append(current)
        transformed = []
        for c in cells:
            if c.startswith('"') and c.endswith('"'):
                inner = c[1:-1]
                transformed.append('"' + transform_text(inner, mode) + '"')
            else:
                transformed.append(transform_text(c, mode))
        result.append(','.join(transformed))
    return '\n'.join(result)

def convert_md(filepath, mode):
    with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
        return transform_text(f.read(), mode)


# =============================================================================
# 4. EPUB with font embedding
# =============================================================================
def embed_font_into_epub(tmpdir, font_path):
    """
    Embed a TTF/OTF font into an EPUB.

    Strategy:
      1. Find the directory where XHTML files live (usually OEBPS/)
      2. Copy font into OEBPS/fonts/
      3. Create OEBPS/vens-font.css with @font-face + body styling
      4. Inject <link> to vens-font.css into every XHTML <head>
      5. Register font + CSS in OPF <manifest>

    Why this approach works across EPUB structures:
      - XHTML files, CSS, and fonts all live in the same base dir (OEBPS/),
        so relative paths are simple: href="vens-font.css", url("fonts/xxx.ttf")
      - CSS with !important overrides publisher styles
      - OPF manifest items use standard media-types recognized by readers
    """

    # --- Step 1: locate the XHTML content directory ---
    # All XHTML files are typically under one root. Find it from the first .xhtml file.
    xhtml_dir = None
    for root, dirs, files in os.walk(tmpdir):
        for f in files:
            if f.endswith(('.xhtml', '.html', '.htm')):
                xhtml_dir = root
                break
        if xhtml_dir:
            break

    if not xhtml_dir:
        return  # no XHTML files found, nothing to do

    font_name = os.path.basename(font_path)
    font_family = os.path.splitext(font_name)[0]

    # --- Step 2: copy font into fonts/ subdirectory ---
    fonts_dir = os.path.join(xhtml_dir, 'fonts')
    os.makedirs(fonts_dir, exist_ok=True)
    shutil.copy2(font_path, os.path.join(fonts_dir, font_name))

    # --- Step 3: create @font-face CSS ---
    # Placed at the SAME level as XHTML files, so relative links are simple
    css_path = os.path.join(xhtml_dir, 'vens-font.css')
    with open(css_path, 'w', encoding='utf-8') as f:
        f.write('@font-face {\n')
        f.write(f'  font-family: "{font_family}";\n')
        f.write(f'  src: url("fonts/{font_name}") format("truetype");\n')
        f.write( '  font-weight: normal;\n')
        f.write( '  font-style: normal;\n')
        f.write('}\n')
        f.write('@font-face {\n')
        f.write(f'  font-family: "{font_family}";\n')
        f.write(f'  src: url("fonts/{font_name}") format("truetype");\n')
        f.write( '  font-weight: bold;\n')
        f.write( '  font-style: normal;\n')
        f.write('}\n')
        f.write('@font-face {\n')
        f.write(f'  font-family: "{font_family}";\n')
        f.write(f'  src: url("fonts/{font_name}") format("truetype");\n')
        f.write( '  font-weight: normal;\n')
        f.write( '  font-style: italic;\n')
        f.write('}\n')
        f.write('@font-face {\n')
        f.write(f'  font-family: "{font_family}";\n')
        f.write(f'  src: url("fonts/{font_name}") format("truetype");\n')
        f.write( '  font-weight: bold;\n')
        f.write( '  font-style: italic;\n')
        f.write('}\n')
        f.write('body {\n')
        f.write(f'  font-family: "{font_family}", serif !important;\n')
        f.write('}\n')
        f.write('p, div, span, h1, h2, h3, h4, h5, h6, li, td, th, blockquote {\n')
        f.write(f'  font-family: "{font_family}", serif !important;\n')
        f.write('}\n')

    # --- Step 4: inject <link> into every XHTML <head> ---
    css_link_tag = f'<link href="vens-font.css" rel="stylesheet" type="text/css"/>'
    for root, dirs, files in os.walk(tmpdir):
        for fname in files:
            if fname.endswith(('.xhtml', '.html', '.htm')):
                fpath = os.path.join(root, fname)
                with open(fpath, 'r', encoding='utf-8', errors='replace') as fh:
                    html = fh.read()
                if css_link_tag not in html:
                    if '</head>' in html:
                        html = html.replace('</head>', f'  {css_link_tag}\n</head>')
                    elif '<head>' in html:
                        html = html.replace('<head>', f'<head>\n  {css_link_tag}')
                    with open(fpath, 'w', encoding='utf-8') as fh:
                        fh.write(html)

    # --- Step 5: register font + CSS in OPF manifest ---
    # Compute paths relative to OPF location
    opf_path = None
    for root, dirs, files in os.walk(tmpdir):
        for f in files:
            if f.endswith('.opf'):
                opf_path = os.path.join(root, f)
                break
        if opf_path:
            break

    if not opf_path:
        return

    opf_dir = os.path.dirname(opf_path)
    font_rel = os.path.relpath(os.path.join(fonts_dir, font_name), opf_dir).replace('\\', '/')
    css_rel = os.path.relpath(css_path, opf_dir).replace('\\', '/')

    with open(opf_path, 'r', encoding='utf-8') as f:
        opf = f.read()

    font_item = f'  <item href="{font_rel}" id="vens-font" media-type="application/x-font-ttf"/>'
    css_item  = f'  <item href="{css_rel}" id="vens-css" media-type="text/css"/>'

    if '</manifest>' in opf:
        opf = opf.replace('</manifest>', f'{font_item}\n{css_item}\n</manifest>')

    with open(opf_path, 'w', encoding='utf-8') as f:
        f.write(opf)

def convert_epub(filepath, mode, font_path=None):
    """Convert EPUB: extract, transform text, embed font, repack."""
    tmpdir = tempfile.mkdtemp()
    try:
        with zipfile.ZipFile(filepath, 'r') as z:
            z.extractall(tmpdir)

        # Transform all xhtml/html files
        for root, dirs, files in os.walk(tmpdir):
            for fname in files:
                if fname.endswith(('.xhtml', '.html', '.htm', '.xml')):
                    fpath = os.path.join(root, fname)
                    with open(fpath, 'r', encoding='utf-8', errors='replace') as f:
                        content = f.read()
                    content = transform_text(content, mode)
                    with open(fpath, 'w', encoding='utf-8') as f:
                        f.write(content)

        # Embed font if specified
        if font_path and os.path.exists(font_path):
            embed_font_into_epub(tmpdir, font_path)

        # Build output filename
        mode_name = 'vens-a' if mode == 'a' else ('vens-b' if mode == 'b' else 'vens-c')
        ts = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
        base = os.path.splitext(os.path.basename(filepath))[0]
        outname = f'{base}_{mode_name}_{ts}.epub'
        outpath = os.path.join(OUTPUT_DIR, outname)

        # Repack
        with zipfile.ZipFile(outpath, 'w', zipfile.ZIP_DEFLATED) as zout:
            mimetype_path = os.path.join(tmpdir, 'mimetype')
            if os.path.exists(mimetype_path):
                zout.write(mimetype_path, 'mimetype', zipfile.ZIP_STORED)
            for root, dirs, files in os.walk(tmpdir):
                for fname in files:
                    fpath = os.path.join(root, fname)
                    arcname = os.path.relpath(fpath, tmpdir)
                    if arcname == 'mimetype':
                        continue
                    zout.write(fpath, arcname, zipfile.ZIP_DEFLATED)
        return outpath
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def convert_file(filepath, mode, font_path=None):
    """Route to correct converter. Returns output file path."""
    ext = os.path.splitext(filepath)[1].lower()
    base = os.path.splitext(os.path.basename(filepath))[0]
    mode_name = 'vens-a' if mode == 'a' else ('vens-b' if mode == 'b' else 'vens-c')
    ts = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
    outname = f'{base}_{mode_name}_{ts}{ext}'
    outpath = os.path.join(OUTPUT_DIR, outname)

    if ext == '.epub':
        return convert_epub(filepath, mode, font_path)
    elif ext == '.csv':
        content = convert_csv(filepath, mode)
    elif ext in ('.md', '.markdown'):
        content = convert_md(filepath, mode)
    else:
        content = convert_txt(filepath, mode)

    with open(outpath, 'w', encoding='utf-8') as f:
        f.write(content)
    return outpath


# =============================================================================
# 5. GUI Application
# =============================================================================
class VENSTransformer:
    def __init__(self, root):
        self.root = root
        self.root.title('VENS Transformer')
        self.root.geometry('700x520')
        self.root.minsize(550, 420)

        self.bg = '#f5f5f5'
        self.fg = '#333333'
        self.accent = '#2563eb'
        self.root.configure(bg=self.bg)

        self.build_ui()
        self.update_status('Ready. Select a file to begin.')

    def build_ui(self):
        main = tk.Frame(self.root, bg=self.bg, padx=30, pady=20)
        main.pack(fill=tk.BOTH, expand=True)

        # ---- Title ----
        title = tk.Label(main, text='VENS Transformer', font=('Segoe UI', 16, 'bold'),
                         bg=self.bg, fg=self.accent)
        title.pack(anchor='w', pady=(0, 5))

        subtitle = tk.Label(main,
            text='Convert English text documents to VENS (Vietnamese-Enhanced Notation System) orthography.',
            font=('Segoe UI', 9), bg=self.bg, fg='#666666', wraplength=620, justify='left')
        subtitle.pack(anchor='w', pady=(0, 20))

        # ---- File Input ----
        file_frame = tk.LabelFrame(main, text='Input File', font=('Segoe UI', 10),
                                    bg=self.bg, fg=self.fg, padx=12, pady=10)
        file_frame.pack(fill=tk.X, pady=(0, 12))

        file_row = tk.Frame(file_frame, bg=self.bg)
        file_row.pack(fill=tk.X)

        self.file_var = tk.StringVar()
        self.file_entry = tk.Entry(file_row, textvariable=self.file_var,
                                    font=('Segoe UI', 10), relief='solid', borderwidth=1)
        self.file_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=4)

        browse_btn = tk.Button(file_row, text='Browse...', command=self.browse_file,
                               bg='#e0e0e0', fg=self.fg, font=('Segoe UI', 9),
                               relief='flat', cursor='hand2', padx=14, pady=4)
        browse_btn.pack(side=tk.LEFT, padx=(8, 0))

        hint = tk.Label(file_frame, text='Supports: TXT, MD, CSV, EPUB  |  Output saved to app folder with timestamp',
                        font=('Segoe UI', 9), bg=self.bg, fg='#999999')
        hint.pack(anchor='w', pady=(4, 0))

        # ---- Conversion Mode (radio buttons, mutually exclusive) ----
        mode_frame = tk.LabelFrame(main, text='Conversion Mode (choose one)', font=('Segoe UI', 10),
                                    bg=self.bg, fg=self.fg, padx=12, pady=10)
        mode_frame.pack(fill=tk.X, pady=(0, 12))

        self.mode_var = tk.StringVar(value='a')

        # Example forms of "vietnamese" for each mode
        ex_a = 'VENS-A  (vietnamese → vïẽtnầmếse)'
        ex_b = 'VENS-B  (vietnamese → vïẽtnầmếsḛ)'
        ex_c = 'VENS-C  (vietnamese → vïẽ̳tnầmế̱sḛ)'

        rb_a = tk.Radiobutton(mode_frame, text=ex_a,
                               variable=self.mode_var, value='a',
                               font=('Segoe UI', 10), bg=self.bg, fg=self.fg,
                               activebackground=self.bg, selectcolor=self.bg, cursor='hand2')
        rb_a.pack(anchor='w')

        rb_b = tk.Radiobutton(mode_frame, text=ex_b,
                               variable=self.mode_var, value='b',
                               font=('Segoe UI', 10), bg=self.bg, fg=self.fg,
                               activebackground=self.bg, selectcolor=self.bg, cursor='hand2')
        rb_b.pack(anchor='w', pady=(4, 0))

        rb_c = tk.Radiobutton(mode_frame, text=ex_c,
                               variable=self.mode_var, value='c',
                               font=('Segoe UI', 10), bg=self.bg, fg=self.fg,
                               activebackground=self.bg, selectcolor=self.bg, cursor='hand2')
        rb_c.pack(anchor='w', pady=(4, 0))

        # ---- Font Selection ----
        font_frame = tk.LabelFrame(main, text='Embedded Font (for EPUB output)', font=('Segoe UI', 10),
                                    bg=self.bg, fg=self.fg, padx=12, pady=10)
        font_frame.pack(fill=tk.X, pady=(0, 12))

        font_row = tk.Frame(font_frame, bg=self.bg)
        font_row.pack(fill=tk.X)

        tk.Label(font_row, text='Font:', font=('Segoe UI', 10), bg=self.bg, fg=self.fg).pack(side=tk.LEFT)

        self.fonts = self.scan_fonts()
        font_names = ['(none)'] + list(self.fonts.keys())
        self.font_var = tk.StringVar(value='(none)')
        self.font_dropdown = ttk.Combobox(font_row, textvariable=self.font_var,
                                           values=font_names, state='readonly',
                                           font=('Segoe UI', 10), width=30)
        self.font_dropdown.pack(side=tk.LEFT, padx=(8, 0))
        self.font_dropdown.current(0)

        font_hint = tk.Label(font_frame,
            text=f'{len(self.fonts)} font(s) found. Selected font will be embedded into EPUB output.',
            font=('Segoe UI', 9), bg=self.bg, fg='#999999')
        font_hint.pack(anchor='w', pady=(4, 0))

        # ---- Convert Button ----
        btn_frame = tk.Frame(main, bg=self.bg)
        btn_frame.pack(fill=tk.X, pady=(8, 12))

        self.convert_btn = tk.Button(btn_frame, text='Convert', command=self.start_conversion,
                                      bg=self.accent, fg='white', font=('Segoe UI', 11, 'bold'),
                                      relief='flat', cursor='hand2', padx=30, pady=8)
        self.convert_btn.pack(side=tk.LEFT)

        # ---- Status Bar ----
        self.status_var = tk.StringVar()
        status_bar = tk.Label(main, textvariable=self.status_var, font=('Segoe UI', 9),
                              bg='#e8e8e8', fg=self.fg, anchor='w', padx=10, pady=6)
        status_bar.pack(fill=tk.X, side=tk.BOTTOM)

        # ---- Progress Bar ----
        self.progress = ttk.Progressbar(main, mode='indeterminate')
        self.progress.pack(fill=tk.X, side=tk.BOTTOM, pady=(0, 4))

    def scan_fonts(self):
        fonts = {}
        if FONTS_DIR.exists():
            for f in sorted(FONTS_DIR.iterdir()):
                if f.suffix.lower() in ('.ttf', '.otf'):
                    fonts[f.name] = str(f)
        return fonts

    def browse_file(self):
        filepath = filedialog.askopenfilename(
            title='Select File to Convert',
            filetypes=[
                ('Supported Files', '*.txt *.md *.csv *.epub'),
                ('Text Files', '*.txt'),
                ('Markdown Files', '*.md'),
                ('CSV Files', '*.csv'),
                ('EPUB Files', '*.epub'),
                ('All Files', '*.*'),
            ]
        )
        if filepath:
            self.file_var.set(filepath)

    def update_status(self, msg):
        self.status_var.set(msg)
        self.root.update_idletasks()

    def start_conversion(self):
        filepath = self.file_var.get().strip()
        if not filepath:
            messagebox.showwarning('No File', 'Please select or enter a file path.')
            return
        if not os.path.exists(filepath):
            messagebox.showerror('File Not Found', f'File not found:\n{filepath}')
            return

        mode = self.mode_var.get()
        font_path = self.fonts.get(self.font_var.get(), None)

        self.convert_btn.config(state='disabled', text='Converting...')
        self.progress.start()
        thread = Thread(target=self.run_conversion, args=(filepath, mode, font_path), daemon=True)
        thread.start()

    def run_conversion(self, filepath, mode, font_path):
        try:
            mode_name = 'VENS-A' if mode == 'a' else ('VENS-B' if mode == 'b' else 'VENS-C')
            self.root.after(0, lambda: self.update_status(f'Converting to {mode_name}...'))
            outpath = convert_file(filepath, mode, font_path)
            self.root.after(0, lambda: self.conversion_done(outpath))
        except Exception as e:
            import traceback
            self.root.after(0, lambda: self.conversion_error(str(e) + '\n' + traceback.format_exc()))

    def conversion_done(self, outpath):
        self.progress.stop()
        self.convert_btn.config(state='normal', text='Convert')
        self.update_status(f'Done! Saved to: {outpath}')
        messagebox.showinfo('Conversion Complete', f'File saved to:\n{outpath}')

    def conversion_error(self, msg):
        self.progress.stop()
        self.convert_btn.config(state='normal', text='Convert')
        self.update_status('Error occurred during conversion.')
        messagebox.showerror('Conversion Error', msg)


# =============================================================================
# 6. Main
# =============================================================================
if __name__ == '__main__':
    root = tk.Tk()
    root.update_idletasks()
    w, h = 700, 550
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    root.geometry(f'{w}x{h}+{(sw-w)//2}+{(sh-h)//2}')
    app = VENSTransformer(root)
    root.mainloop()
