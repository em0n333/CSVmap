#!/usr/bin/env python3
"""ReconSheet - tiny, fast CSV viewer/editor for huge recon files (httpx etc).
Deps: pyside6, numpy.   Run: python reconcsv.py [file.csv ...]
Files are memory-mapped; only a row-offset index lives in RAM (~8 bytes/row)."""
import sys, os, re, csv, io, json, mmap, tempfile, shutil, hashlib
from string import Template
import numpy as np
from PySide6 import QtCore as C, QtGui as G, QtWidgets as W
from PySide6.QtSvg import QSvgRenderer

# ---------------------------------------------------------------- config (~/.config/csvmap)
CONF_DIR = os.path.join(os.environ.get('XDG_CONFIG_HOME') or os.path.expanduser('~/.config'), 'csvmap')
SETTINGS_PATH = os.path.join(CONF_DIR, 'config', 'settings.json')
COLORMAP_DIR = os.path.join(CONF_DIR, 'colormaps')


def _load_settings():
    try:
        with open(SETTINGS_PATH, encoding='utf-8') as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


SETTINGS = _load_settings()


def get_setting(key, default=None):
    return SETTINGS.get(key, default)


def set_setting(key, value):
    """value None removes the key. Written atomically (tmp file + replace) so a crash can't corrupt it."""
    if value is None:
        SETTINGS.pop(key, None)
    else:
        SETTINGS[key] = value
    try:
        os.makedirs(os.path.dirname(SETTINGS_PATH), exist_ok=True)
        tmp = SETTINGS_PATH + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(SETTINGS, f, indent=2)
        os.replace(tmp, SETTINGS_PATH)
    except OSError:
        pass


def colormap_paths(csv_path):
    """(marks, cells) json paths for a csv. The id is a hash of the file's absolute path: unique per file,
    but stable, so the same file finds its colours again next time (a random/timestamp id could not)."""
    ap = os.path.abspath(csv_path)
    base = '%s-%s' % (os.path.basename(ap), hashlib.sha1(ap.encode('utf-8', 'surrogateescape')).hexdigest()[:10])
    return (os.path.join(COLORMAP_DIR, base + '.marks.json'), os.path.join(COLORMAP_DIR, base + '.cells.json'))


TINT = {'red': '#FDE4E4', 'orange': '#FDF1DE', 'green': '#E1F4E7', 'blue': '#E3EDFC'}
INK = {'red': '#EF4444', 'orange': '#F59E0B', 'green': '#22C55E', 'blue': '#3B82F6'}


TINT_RGB = {'red': (230, 40, 70), 'orange': (245, 158, 11), 'green': (16, 185, 129), 'blue': (0, 126, 217)}
BORDQ = {k: G.QColor(r, g, b, 235) for k, (r, g, b) in TINT_RGB.items()}      # thin outline of a mark


def tints(alpha):                    # marks: vivid colours drawn translucent (alpha 0-255) over the background
    return {k: G.QColor(r, g, b, alpha) for k, (r, g, b) in TINT_RGB.items()}


TINTQ = tints(50)
INKQ = {k: G.QColor(v) for k, v in INK.items()}
STATUS_INK = {'2': INKQ['green'], '3': INKQ['blue'], '4': INKQ['orange'], '5': INKQ['red']}
DARK, MUTED, WHITE = G.QColor('#111827'), G.QColor('#6B7280'), G.QColor('#FFFFFF')
GRID = G.QColor('#EEF0F3')
MUTED_HDR = ('tech', 'ip', 'host', 'len', 'length', 'time', 'webserver', 'cname', 'cdn', 'words', 'lines')

ROW_H = 33
ALIGN = C.Qt.AlignVCenter | C.Qt.AlignLeft
F_RO = C.Qt.ItemIsEnabled | C.Qt.ItemIsSelectable
F_RW = F_RO | C.Qt.ItemIsEditable
ELIDE = C.Qt.ElideRight
SELECTED = W.QStyle.State_Selected
PLAIN = C.Qt.PlainText

# ---------------------------------------------------------------- themes
THEMES = {
    'light': dict(name='light', bg='#FFFFFF', text='#111827', muted='#6B7280', grid='#EEF0F3',
                  hdr_bg='#F6F7F9', hdr_fg='#6B7280', line='#E5E7EB', hover='#F3F4F6', checked='#E3EDFC',
                  field='#F6F7F9', handle='#D1D5DB', handle_hover='#9CA3AF', icon='#1F2937', dot_hover='#D1D5DB', alpha=50),
    'dark': dict(name='dark', bg='#000000', text='#E5E7EB', muted='#8B93A1', grid='#1A1A1A',
                 hdr_bg='#0A0A0A', hdr_fg='#9AA3B2', line='#1F1F1F', hover='#1A1A1A', checked='#1E2F4D',
                 field='#0A0A0A', handle='#3A404B', handle_hover='#4E5666', icon='#D1D5DB', dot_hover='#4B5563', alpha=85),
}
CUR = THEMES['light']
MOON = '<path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/>'
SUN = ('<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4'
       'M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>')


def make_palette(t):
    p = G.QPalette(W.QApplication.style().standardPalette())
    bg, tx, mu, fld = (G.QColor(t[k]) for k in ('bg', 'text', 'muted', 'field'))
    for role in (G.QPalette.Window, G.QPalette.Base, G.QPalette.AlternateBase):
        p.setColor(role, bg)
    p.setColor(G.QPalette.Button, fld)
    for role in (G.QPalette.WindowText, G.QPalette.Text, G.QPalette.ButtonText, G.QPalette.ToolTipText):
        p.setColor(role, tx)
    p.setColor(G.QPalette.ToolTipBase, fld)
    p.setColor(G.QPalette.PlaceholderText, mu)
    for role in (G.QPalette.WindowText, G.QPalette.Text, G.QPalette.ButtonText):
        p.setColor(G.QPalette.Disabled, role, mu)
    return p


def apply_theme(name):
    """Swap the colour globals the delegate reads at paint time, plus the app palette."""
    global CUR, TINTQ, DARK, MUTED, WHITE, GRID
    CUR = THEMES[name]
    TINTQ = tints(CUR['alpha'])
    DARK, MUTED, WHITE, GRID = (G.QColor(CUR[k]) for k in ('text', 'muted', 'bg', 'grid'))
    app = W.QApplication.instance()
    if app:
        app.setPalette(make_palette(CUR))


# ---------------------------------------------------------------- data layer
class CsvData:
    def __init__(self, path, notes=False):
        self.path, self.edits, self.cache = path, {}, {}
        self.notes = notes                          # True: auto-add a virtual 'notes' column when the file has none
        self.load()

    def load(self):
        self.f = open(self.path, 'rb')
        self.size = os.fstat(self.f.fileno()).st_size
        if not self.size:
            raise ValueError('File is empty')
        self.mm = mmap.mmap(self.f.fileno(), 0, access=mmap.ACCESS_READ)
        parts, CH = [np.zeros(1, np.int64)], 1 << 26
        for b in range(0, self.size, CH):
            a = np.frombuffer(self.mm, np.uint8, min(CH, self.size - b), b)
            parts.append(np.flatnonzero(a == 10) + b + 1)
            del a
        st = np.concatenate(parts)
        if st[-1] >= self.size:
            st = st[:-1]
        self.st = self.join_multiline(np.append(st, self.size))   # record i = mm[st[i]:st[i+1]]
        self.n = len(self.st) - 2                   # data rows (minus header)
        h = self.line(0)
        self.d = max(',;\t|', key=h.count)
        head = self.parse(h)
        self.virt = self.notes and 'notes' not in [x.lower() for x in head]
        self.cols = head + (['notes'] if self.virt else [])
        self.cache.clear()
        self.ns = self.n                            # rows that live in the file; ids >= ns are new rows
        self.nat = self.pos = None                  # file order / id -> position (only once rows are added or deleted)
        self.dcols, self.corder = set(), list(range(len(self.cols)))   # deleted columns / file column order
        self.cols_dirty = self.struct = False
        self.nc0 = len(self.cols)                   # columns that exist on disk

    def join_multiline(self, st):
        """Merge physical lines that belong to one record (a quoted field containing line breaks).
        Vectorised: only lines with an ODD number of quotes can open/close a quoted field, so the
        Python-level work is limited to those (none at all for most files)."""
        mm, size = self.mm, self.size
        if mm.find(b'"') < 0:
            return st
        nl = len(st) - 1
        qc = np.zeros(nl, np.int64)
        CH = 1 << 26
        for b in range(0, size, CH):
            a = np.frombuffer(mm, np.uint8, min(CH, size - b), b)
            q = np.flatnonzero(a == 34) + b
            del a
            if len(q):
                li = np.searchsorted(st, q, side='right') - 1
                lo = int(li[0])
                qc[lo:int(li[-1]) + 1] += np.bincount(li - lo)
        odd = np.flatnonzero(qc & 1).tolist()
        if not odd:
            return st
        d = ord(self.d_guess())
        keep, k = np.ones(len(st), bool), 0
        while k < len(odd):
            i = odd[k]
            line = mm[st[i]:st[i + 1]]
            if self._opens(line, d) and k + 1 < len(odd):    # quote opened and not closed on this line ->
                j = odd[k + 1]                                # the next odd line closes it
                keep[i + 1:j + 1] = False
                k += 2
            else:                                             # stray quote (e.g. inside an unquoted url)
                k += 1
        return st[keep]

    def d_guess(self):
        h = self.mm[:4096].decode('utf-8', 'replace')
        return max(',;\t|', key=h.count)

    @staticmethod
    def _opens(line, d):
        """True if scanning this line (starting outside quotes) ends inside an open quoted field.
        A quote only opens a field at a field start, so `foo"bar` is just text."""
        inq, prev, i, n = False, 10, 0, len(line)
        while i < n:
            ch = line[i]
            if inq:
                if ch == 34:
                    if i + 1 < n and line[i + 1] == 34:
                        i += 1
                    else:
                        inq = False
            elif ch == 34 and (prev == d or prev == 10):
                inq = True
            prev = ch; i += 1
        return inq

    def close(self):
        self.mm.close(); self.f.close()

    def raw(self, i): return self.mm[self.st[i]:self.st[i + 1]]
    def line(self, i): return self.raw(i).rstrip(b'\r\n').decode('utf-8', 'replace')

    def parse(self, s):
        # fast path: no quotes -> plain split (10x+ faster than csv.reader)
        if '"' not in s:
            return s.split(self.d)
        r = list(csv.reader([s], delimiter=self.d))
        return r[0] if r else []

    def row(self, r):
        v = self.cache.get(r)
        if v is None:
            if len(self.cache) > 2000:
                self.cache.clear()
            v = self.cache[r] = self.parse(self.line(r + 1)) if r < self.ns else []
        return v

    def field(self, r, c):
        if self.edits:
            e = self.edits.get((r, c))
            if e is not None:
                return e
        v = self.row(r)
        return v[c] if c < len(v) else ''

    def sort_order(self, c):
        """argsort (ascending) of ALL data rows by column c. Heavy: called once per column, then cached."""
        n = self.n
        if n == 0:
            return np.zeros(0, np.int64)
        d, mm, st = self.d.encode(), self.mm, self.st.tolist()
        vals, ap = [], None
        ap = vals.append
        for i in range(1, self.ns + 1):
            raw = mm[st[i]:st[i + 1]].rstrip(b'\r\n')
            if b'"' in raw:
                p = [x.encode() for x in self.parse(raw.decode('utf-8', 'replace'))]
            else:
                p = raw.split(d)
            ap(p[c][:64] if c < len(p) else b'')      # cap key length so the numpy array stays small
        vals.extend([b''] * (n - self.ns))               # new rows: values come from edits only
        for (r, cc), v in self.edits.items():
            if cc == c:
                vals[r] = v.encode()[:64]
        arr = np.array(vals)
        try:
            key = np.where(arr == b'', b'nan', arr).astype(np.float64)   # numeric, blanks last
            return np.argsort(key, kind='stable')
        except ValueError:
            return np.argsort(np.char.lower(arr), kind='stable')

    # ---- structure edits. Row ids never change until save: ids < ns are file rows, ids >= ns are new
    # rows whose cells live only in `edits`. `nat` = live row ids in file order (None = untouched file).
    @property
    def live(self): return self.n if self.nat is None else len(self.nat)

    def changed(self): return bool(self.edits or self.struct)

    def natpos(self, r): return r if self.pos is None else int(self.pos[r])

    def num(self, r): return str(r + 1 if self.pos is None else int(self.pos[r]) + 1)

    def _order(self):
        self.pos = np.full(self.n, -1, np.int64)
        self.pos[self.nat] = np.arange(len(self.nat), dtype=np.int64)
        self.struct = True

    def insert_row(self, at):
        if self.nat is None:
            self.nat = np.arange(self.n, dtype=np.int64)
        r = self.n; self.n += 1
        self.nat = np.insert(self.nat, at, r)
        self._order()
        return r

    def delete_rows(self, ids):
        if self.nat is None:
            self.nat = np.arange(self.n, dtype=np.int64)
        gone = set(map(int, ids))
        self.nat = self.nat[~np.isin(self.nat, np.fromiter(gone, np.int64, len(gone)))]
        self.edits = {k: v for k, v in self.edits.items() if k[0] not in gone}
        self._order()

    def add_col(self, name, ref=None, right=True):
        c = len(self.cols); self.cols.append(name)
        self.corder.insert(len(self.corder) if ref is None else self.corder.index(ref) + right, c)
        self.cols_dirty = self.struct = True
        return c

    def rename_col(self, c, name):
        self.cols[c] = name; self.cols_dirty = self.struct = True

    def delete_col(self, c):
        self.dcols.add(c); self.cols_dirty = self.struct = True

    def fmt(self, f):
        s = self.d.join(f)                       # fast path: nothing needs quoting -> plain join
        if len(f) > 1 and '"' not in s and '\n' not in s and '\r' not in s and s.count(self.d) == len(f) - 1:
            return (s + '\n').encode()
        buf = io.StringIO()
        csv.writer(buf, delimiter=self.d, lineterminator='\n').writerow(f)
        return buf.getvalue().encode()

    def _ids(self):
        if self.nat is None:
            return range(self.ns)
        return (i for k in range(0, len(self.nat), 1 << 20) for i in self.nat[k:k + (1 << 20)].tolist())

    def save(self):
        byrow, nc, dl = {}, len(self.cols), self.d.encode()
        for (r, c), v in self.edits.items():
            byrow.setdefault(r, {})[c] = v
        keep = [c for c in self.corder if c not in self.dcols]
        cd, ns = self.cols_dirty, self.ns
        tmp = self.path + '.rs_tmp'
        with open(tmp, 'wb') as o:
            h = self.raw(0)
            if cd:
                h = self.fmt([self.cols[c] for c in keep])
            elif self.virt:
                h = h.rstrip(b'\r\n') + dl + b'notes\n'
            o.write(h if h.endswith(b'\n') else h + b'\n')
            for r in self._ids():
                ed = byrow.get(r)
                if r < ns and ed is None and not cd:       # untouched file row: copy bytes as-is
                    raw = self.raw(r + 1)
                    if self.virt:
                        raw = raw.rstrip(b'\r\n') + dl + b'\n'
                    elif not raw.endswith(b'\n'):
                        raw += b'\n'
                else:
                    f = self.parse(self.line(r + 1)) if r < ns else []
                    f += [''] * (nc - len(f))
                    for c, v in (ed or {}).items():
                        f[c] = v
                    raw = self.fmt([f[c] for c in keep] if cd else f)
                o.write(raw)
        self.close(); os.replace(tmp, self.path); self.edits = {}
        self.load()


class Model(C.QAbstractTableModel):
    edited = C.Signal()
    shape = C.Signal()

    def __init__(s, d):
        super().__init__()
        s.d, s.view, s.matches, s.marked_only = d, None, None, False
        s.sortc, s.asc, s.query, s.marks = None, True, '', {}
        s.ranks = {}                                  # col -> rank array (row -> sorted position)
        s.undo, s.redo, s.replaying = [], [], False   # undo/redo history (recorded by push, replayed by Tab.undo/redo)
        s.retag()
        s.mpath, s.cpath = colormap_paths(d.path)
        for p in (s.mpath, d.path + '.marks.json'):          # new location first, then the legacy sidecar file
            if os.path.exists(p):
                try:
                    s.marks = {int(k): v for k, v in json.load(open(p)).items() if v in TINT and int(k) < d.n}
                except Exception:
                    pass
                break
        s.cmarks = {}
        for p in (s.cpath, d.path + '.cells.json'):
            if os.path.exists(p):
                try:
                    s.cmarks = {(r, c): v for r, c, v in json.load(open(p)) if v in TINT and r < d.n and c < len(d.cols)}
                except Exception:
                    pass
                break

    def retag(s):
        s.stat = {i for i, h in enumerate(s.d.cols) if 'status' in h.lower()}
        s.mute = {i for i, h in enumerate(s.d.cols) if h.lower() in MUTED_HDR}

    def save_marks(s):
        try:
            mk = {k: v for k, v in s.marks.items() if k < s.d.ns}     # new (unsaved) rows have no line yet
            ck = [[r, c, v] for (r, c), v in s.cmarks.items() if r < s.d.ns and c < s.d.nc0]
            if mk or ck:
                os.makedirs(COLORMAP_DIR, exist_ok=True)
            for data, path, legacy in ((mk, s.mpath, '.marks.json'), (ck, s.cpath, '.cells.json')):
                if data:
                    with open(path, 'w') as f:
                        json.dump(data, f)
                elif os.path.exists(path):
                    os.remove(path)
                old = s.d.path + legacy                     # old-style file next to the csv: migrated, remove it
                if os.path.exists(old):
                    os.remove(old)
        except OSError:
            pass

    def rowCount(s, p=C.QModelIndex()):
        return 0 if p.isValid() else (s.d.n if s.view is None else len(s.view))

    def columnCount(s, p=C.QModelIndex()):
        return 0 if p.isValid() else len(s.d.cols) + 1

    def src(s, row): return row if s.view is None else int(s.view[row])

    def src_range(s, top, bot):
        return range(top, bot + 1) if s.view is None else s.view[top:bot + 1].tolist()

    def headerData(s, sec, o, role=C.Qt.DisplayRole):
        if o == C.Qt.Horizontal and role == C.Qt.DisplayRole:
            # return '#' if sec == 0 else s.d.cols[sec - 1].upper().replace('\r', '')
            return '#' if sec == 0 else s.d.cols[sec - 1].replace('\r', '')

    def flags(s, ix): return F_RW if ix.column() > 0 else F_RO

    def data(s, ix, role=C.Qt.DisplayRole):
        # painting is done entirely by the delegate (it reads the model directly),
        # so only the roles Qt itself needs are answered here.
        if role == C.Qt.DisplayRole:
            c = ix.column(); r = s.src(ix.row())
            return s.d.num(r) if c == 0 else s.d.field(r, c - 1)
        if role == C.Qt.EditRole:
            c = ix.column()
            return s.d.field(s.src(ix.row()), c - 1) if c else None

    def setData(s, ix, v, role=C.Qt.EditRole):
        if role != C.Qt.EditRole or ix.column() == 0:
            return False
        r, c = s.src(ix.row()), ix.column() - 1
        if v == s.d.field(r, c):
            return False
        s.push(['cell', r, c, s.d.edits.get((r, c)), v])
        s.d.edits[(r, c)] = v
        s.ranks.pop(c, None)
        s.dataChanged.emit(ix, ix); s.edited.emit()
        return True

    # ---- rows / columns (incremental: scroll position and selection survive)
    def _plain(s): return s.sortc is None and s.matches is None and not s.marked_only

    def _repaint(s):
        n = s.rowCount()
        if n:
            s.dataChanged.emit(s.index(0, 0), s.index(n - 1, s.columnCount() - 1))   # '#' numbers shift
        s.shape.emit(); s.edited.emit()

    def insert_row(s, v, below=True):
        """New blank row next to view row v (or at the end of the file if nothing is shown). Returns its view row."""
        d = s.d
        if s.rowCount():
            at, k = d.natpos(s.src(v)) + below, v + below
        else:
            at, k = d.live, 0
        base = np.arange(d.n, dtype=np.int64) if s.view is None else s.view
        fl = (d.cols_dirty, d.struct)
        s.beginInsertRows(C.QModelIndex(), k, k)
        rid = d.insert_row(at)
        s.push(['ins', rid, at, fl, None])
        s.ranks.clear()
        s.view = d.nat if s._plain() else np.insert(base, k, rid)
        s.endInsertRows()
        s._repaint()
        return k

    def delete_rows(s, vpos):
        """vpos: sorted unique view positions."""
        d = s.d
        base = np.arange(d.n, dtype=np.int64) if s.view is None else s.view
        s.view = base
        if not s.replaying:
            s.push(s.snap(base[vpos]))
        d.delete_rows(base[vpos])
        for run in reversed(np.split(vpos, np.flatnonzero(np.diff(vpos) > 1) + 1)):
            top, bot = int(run[0]), int(run[-1])
            s.beginRemoveRows(C.QModelIndex(), top, bot)
            s.view = np.delete(s.view, np.s_[top:bot + 1])
            s.endRemoveRows()
        if s._plain():
            s.view = d.nat
        s._repaint()

    def add_col(s, name, ref=None, right=True):
        c = len(s.d.cols)
        fl = (s.d.cols_dirty, s.d.struct)
        s.beginInsertColumns(C.QModelIndex(), c + 1, c + 1)
        s.d.add_col(name, ref, right)
        s.push(['addcol', c, name, ref, right, fl, None, None])
        s.endInsertColumns()
        s.retag(); s.edited.emit()
        return c

    def rename_col(s, c, name):
        s.push(['ren', c, s.d.cols[c], name, (s.d.cols_dirty, s.d.struct)])
        s.d.rename_col(c, name); s.retag()
        s.headerDataChanged.emit(C.Qt.Horizontal, c + 1, c + 1); s.edited.emit()

    def delete_col(s, c):
        s.push(['delcol', c, (s.d.cols_dirty, s.d.struct)])
        s.d.delete_col(c); s.ranks.pop(c, None)
        if s.sortc == c:
            s.sortc = None; s.rebuild()
        s.edited.emit()

    # ---- undo / redo machinery. Row ids never change until a save, so every change is recorded as a small
    # command and replayed strictly last-in-first-out (Tab.undo / Tab.redo). A save clears the history.
    def push(s, rec):
        if s.replaying:
            return
        s.undo.append(rec)
        if len(s.undo) > 1000:
            del s.undo[0]
        s.redo.clear()

    def snap(s, ids):
        """Record of rows about to be deleted: ids + their file positions + their edited cells."""
        d = s.d
        at = ids if d.pos is None else d.pos[ids]
        o = np.argsort(at, kind='stable')
        ids, at = ids[o], at[o]
        gone = set(ids.tolist())
        return ['del', ids, at, {k: v for k, v in d.edits.items() if k[0] in gone}, (d.cols_dirty, d.struct)]

    def vpos1(s, r):
        """View row of row id r, or None if it is not shown."""
        d = s.d
        if s.view is None:
            return r if r < d.n else None
        if s._plain() and d.pos is not None:
            p = int(d.pos[r])
            return p if p >= 0 else None
        w = np.flatnonzero(s.view == r)
        return int(w[0]) if len(w) else None

    def vposs(s, ids):
        """Sorted view rows of the given row ids (those that are shown)."""
        ids = np.asarray(ids, np.int64)
        d = s.d
        if s.view is None:
            return np.sort(ids[ids < d.n])
        if s._plain() and d.pos is not None:
            p = d.pos[ids]
            return np.sort(p[p >= 0])
        return np.flatnonzero(np.isin(s.view, ids))

    def hide(s, ids):
        """Take rows out again (undo of an insert / redo of a delete). Not recorded."""
        d = s.d
        if d.pos is None:
            return
        ids = ids[d.pos[ids] >= 0]
        if not len(ids):
            return
        if s._plain():
            s.delete_rows(np.sort(d.pos[ids]))
        else:
            d.delete_rows(ids); s.rebuild(); s.edited.emit()

    def restore(s, ids, at, ed):
        """Put rows back at their old file positions with their edited cells (undo of a delete / redo of an insert)."""
        d = s.d
        if d.pos is None or not len(ids):
            return
        gone = d.pos[ids] < 0
        ids, at = ids[gone], at[gone]
        if not len(ids):
            return
        d.nat = np.insert(d.nat, at - np.arange(len(at)), ids)
        d._order(); d.edits.update(ed)
        if s._plain() and s.view is not None:
            pos = d.pos[ids]                                   # ascending: ids are sorted by file position
            for run in np.split(np.arange(len(ids)), np.flatnonzero(np.diff(pos) > 1) + 1):
                top, bot = int(pos[run[0]]), int(pos[run[-1]])
                s.beginInsertRows(C.QModelIndex(), top, bot)
                s.view = np.insert(s.view, top, ids[run])
                s.endInsertRows()
            s.view = d.nat
            s._repaint()
        else:
            s.rebuild(); s.edited.emit()

    def drop_col(s, c):
        """Undo of add_col: the column that was added is always the last one."""
        d = s.d
        if c != len(d.cols) - 1:
            return
        s.beginRemoveColumns(C.QModelIndex(), c + 1, c + 1)
        d.cols.pop(); d.corder.remove(c)
        d.edits = {k: v for k, v in d.edits.items() if k[1] != c}
        s.cmarks = {k: v for k, v in s.cmarks.items() if k[1] != c}
        s.endRemoveColumns()
        s.ranks.pop(c, None); s.retag()
        if s.sortc == c:
            s.sortc = None; s.rebuild()
        s.edited.emit()

    # ---- marking
    def cmark(s, rows, c, color):
        for r in rows:
            if color:
                s.cmarks[(r, c)] = color
            else:
                s.cmarks.pop((r, c), None)
        s.dataChanged.emit(s.index(0, 0), s.index(max(s.rowCount() - 1, 0), s.columnCount() - 1))

    def mark(s, rows, color):
        for r in rows:
            if color:
                s.marks[r] = color
            else:
                s.marks.pop(r, None)
        if s.marked_only:
            s.rebuild()
        else:
            s.dataChanged.emit(s.index(0, 0), s.index(max(s.rowCount() - 1, 0), s.columnCount() - 1))

    # ---- search / sort / filter
    def search(s, text):
        s.query = text
        if not text:
            s.matches = None
        else:
            pat = re.compile(re.escape(text.encode()), re.I)
            pos = np.fromiter((m.start() for m in pat.finditer(s.d.mm)), np.int64)
            ln = np.searchsorted(s.d.st, pos, side='right') - 1
            s.matches = np.unique(ln[ln > 0] - 1)
        s.rebuild()

    def rank(s, c):
        r = s.ranks.get(c)
        if r is None:
            o = s.d.sort_order(c)
            r = np.empty(len(o), np.int64)
            r[o] = np.arange(len(o), dtype=np.int64)
            if len(s.ranks) >= 4:
                s.ranks.pop(next(iter(s.ranks)))
            s.ranks[c] = r
        return r

    def rebuild(s):
        rows = s.matches
        if s.marked_only:
            m = np.fromiter(sorted(s.marks), np.int64, len(s.marks))
            rows = m if rows is None else np.intersect1d(rows, m)
        d = s.d
        if d.nat is not None:                     # rows added/deleted: keep file order, hide deleted, always show new rows
            if rows is None:
                rows = d.nat
            else:
                rows = np.union1d(rows, np.arange(d.ns, d.n, dtype=np.int64))
                rows = rows[d.pos[rows] >= 0]
                rows = rows[np.argsort(d.pos[rows], kind='stable')]
        if s.sortc is not None:
            rk = s.rank(s.sortc)
            if rows is None:
                rows = np.empty(len(rk), np.int64)
                rows[rk] = np.arange(len(rk), dtype=np.int64)   # inverse permutation
            else:
                rows = rows[np.argsort(rk[rows], kind='stable')]
            if not s.asc:
                rows = rows[::-1]
        s.beginResetModel(); s.view = rows; s.endResetModel()
        s.shape.emit()


class MultiEdit(W.QPlainTextEdit):
    """Cell / column-name editor: Enter = done, Ctrl+Enter or Shift+Enter = line break.
    Grows downward with its lines (only exists while a cell is being edited -> no cost on the grid)."""
    MAXL = 12

    def __init__(s, parent=None, fit_parent=True):
        super().__init__(parent)
        s.setLineWrapMode(W.QPlainTextEdit.NoWrap)
        s.setHorizontalScrollBarPolicy(C.Qt.ScrollBarAlwaysOff)
        s.setTabChangesFocus(True)
        s.rect_, s.fit_parent = None, fit_parent
        if fit_parent:
            s.textChanged.connect(s.fit)

    def keyPressEvent(s, e):
        if e.key() in (C.Qt.Key_Return, C.Qt.Key_Enter) and e.modifiers() & (C.Qt.ShiftModifier | C.Qt.ControlModifier):
            s.insertPlainText('\n'); e.accept(); return
        super().keyPressEvent(e)

    def insertFromMimeData(s, src):                  # paste: normalise \r\n, keep real line breaks
        if src.hasText():
            s.insertPlainText(src.text().replace('\r\n', '\n').replace('\r', '\n'))
        else:
            super().insertFromMimeData(src)

    def place(s, rect):
        s.rect_ = C.QRect(rect); s.fit()

    def fit(s):
        r = s.rect_
        if r is None or not s.fit_parent:
            return
        n = min(max(s.document().blockCount(), 1), s.MAXL)
        h = max(r.height(), n * s.fontMetrics().lineSpacing() + 14)
        vp = s.parentWidget().height() if s.parentWidget() else h
        y = r.top() if r.top() + h <= vp else max(0, vp - h)
        s.setVerticalScrollBarPolicy(C.Qt.ScrollBarAlwaysOff if s.document().blockCount() <= s.MAXL else C.Qt.ScrollBarAsNeeded)
        s.setGeometry(r.left(), y, max(r.width(), 200), h)


def ask_text(parent, title, label, text=''):
    """Multi-line replacement for QInputDialog.getText: Enter = OK, Ctrl/Shift+Enter = line break."""
    dlg = W.QDialog(parent); dlg.setWindowTitle(title)
    lay = W.QVBoxLayout(dlg); lay.addWidget(W.QLabel(label))
    ed = MultiEdit(fit_parent=False)
    ed.setPlainText(text); ed.selectAll(); ed.setFixedHeight(96); ed.setMinimumWidth(360)
    ed.setObjectName('nameedit')
    ed.setStyleSheet(Template('QPlainTextEdit#nameedit { background: $field; border: 1px solid $line; border-radius: 7px; '
                              'padding: 3px 6px; color: $text; } QPlainTextEdit#nameedit:focus { border: 1px solid #3B82F6; '
                              'background: $bg; }').substitute(CUR))
    ed.setVerticalScrollBarPolicy(C.Qt.ScrollBarAsNeeded)
    ed.setStyleSheet('QPlainTextEdit { background: %s; border: 1px solid %s; border-radius: 7px; padding: 3px 6px; color: %s; }'
                     'QPlainTextEdit:focus { border: 1px solid #3B82F6; background: %s; }'
                     % (CUR['field'], CUR['line'], CUR['text'], CUR['bg']))
    lay.addWidget(ed)
    bb = W.QDialogButtonBox(W.QDialogButtonBox.Ok | W.QDialogButtonBox.Cancel)
    bb.accepted.connect(dlg.accept); bb.rejected.connect(dlg.reject); lay.addWidget(bb)
    class F(C.QObject):
        def eventFilter(f, o, ev):
            if ev.type() == C.QEvent.KeyPress and ev.key() in (C.Qt.Key_Return, C.Qt.Key_Enter) \
                    and not ev.modifiers() & (C.Qt.ShiftModifier | C.Qt.ControlModifier):
                dlg.accept(); return True
            return False
    flt = F(dlg); ed.installEventFilter(flt)
    ok = dlg.exec() == W.QDialog.Accepted
    return ed.toPlainText(), ok


class Delegate(W.QStyledItemDelegate):
    """Paints a cell with the minimum of work. Text widths and laid-out text (QStaticText) are
    cached, so scrolling back/forth and dragging a column edge re-use them instead of re-shaping
    every string on every repaint."""
    SEL = G.QPen(G.QColor('#3B82F6'), 2)
    MAXL = 10                 # most text lines a row grows to show

    def __init__(s, view, model):
        super().__init__(view)
        s.m = model
        s.fn = G.QFont(view.font())
        s.fb = G.QFont(s.fn); s.fb.setBold(True)
        s.fm = G.QFontMetrics(s.fn)
        s.fmb = G.QFontMetrics(s.fb)
        s.lh = s.fm.height()
        s.pgrid = G.QPen(GRID)
        s.adv = ({}, {})          # text -> advance width     (regular, bold)
        s.stc = ({}, {})          # text[, width] -> QStaticText (regular, bold)
        s.ident = G.QTransform()

    def restyle(s):
        s.pgrid = G.QPen(GRID)

    def refont(s, font):
        s.fn = G.QFont(font)
        s.fb = G.QFont(s.fn); s.fb.setBold(True)
        s.fm = G.QFontMetrics(s.fn); s.fmb = G.QFontMetrics(s.fb)
        s.lh = s.fm.height()
        for c in (*s.adv, *s.stc):
            c.clear()

    def createEditor(s, parent, opt, ix):
        e = MultiEdit(parent)
        e.setStyleSheet('border: 2px solid #3B82F6; background: %s; color: %s; padding: 0 4px;'
                        % (CUR['bg'], CUR['text']))
        return e

    def setEditorData(s, e, ix):
        e.setPlainText(ix.data(C.Qt.EditRole) or '')
        e.selectAll()

    def setModelData(s, e, model, ix):
        model.setData(ix, e.toPlainText(), C.Qt.EditRole)

    def updateEditorGeometry(s, e, opt, ix):
        e.place(opt.rect)

    def eventFilter(s, o, ev):
        # Enter commits (like before); Ctrl/Shift+Enter is handled by MultiEdit itself (new line)
        if isinstance(o, MultiEdit) and ev.type() == C.QEvent.KeyPress and ev.key() in (C.Qt.Key_Return, C.Qt.Key_Enter) \
                and not ev.modifiers() & (C.Qt.ShiftModifier | C.Qt.ControlModifier):
            s.commitData.emit(o); s.closeEditor.emit(o, W.QAbstractItemDelegate.NoHint)
            return True
        return super().eventFilter(o, ev)

    def paint(s, p, opt, ix):
        m, c, rect = s.m, ix.column(), opt.rect
        row, v = ix.row(), m.view
        r = row if v is None else int(v[row])
        rk = m.marks.get(r)
        ck = m.cmarks.get((r, c - 1)) if m.cmarks and c else None
        mk = ck or rk                                # a coloured cell wins over its row colour
        p.fillRect(rect, WHITE)
        if mk:
            p.fillRect(rect, TINTQ[mk])               # translucent: blends with the theme background
        p.setPen(s.pgrid)
        p.drawLine(rect.topRight(), rect.bottomRight()); p.drawLine(rect.bottomLeft(), rect.bottomRight())
        if mk:                                       # thin bright outline: rounded box for a cell, one strip for a row
            if ck:
                p.save(); p.setRenderHint(G.QPainter.Antialiasing); p.setPen(BORDQ[ck])
                p.drawRoundedRect(C.QRectF(rect).adjusted(1.5, 1.5, -1.5, -1.5), 3, 3); p.restore()
            else:
                p.setPen(BORDQ[rk])
                p.drawLine(rect.left(), rect.top(), rect.right(), rect.top())
                p.drawLine(rect.left(), rect.bottom(), rect.right(), rect.bottom())
                if c == 0:
                    p.drawLine(rect.left(), rect.top(), rect.left(), rect.bottom())
                if c == m.columnCount() - 1:
                    p.drawLine(rect.right(), rect.top(), rect.right(), rect.bottom())

        if c == 0:
            text, pen, bold = m.d.num(r), (INKQ[mk] if mk else MUTED), True
        else:
            cc = c - 1
            text = m.d.field(r, cc)
            if cc in m.stat:
                pen, bold = STATUS_INK.get(text[:1], DARK), True
            else:
                pen, bold = (MUTED if cc in m.mute else DARK), False
        if text:
            w = rect.width() - 14
            if w > 0:
                b = 1 if bold else 0
                fnt = s.fb if bold else s.fn
                fmx = s.fmb if bold else s.fm
                if '\n' in text or '\r' in text:           # multi-line cell: one line per row of text
                    lines = text[:4000].replace('\r', '').split('\n')[:s.MAXL]
                else:
                    lines = (text[:200],)
                p.setFont(fnt); p.setPen(pen)
                y = rect.top() + (ROW_H - s.lh) // 2
                advc, cache = s.adv[b], s.stc[b]
                for text in lines:
                    if text:
                        text = text[:200]
                        adv = advc.get(text)
                        if adv is None:
                            if len(advc) > 30000:
                                advc.clear()
                            adv = advc[text] = fmx.horizontalAdvance(text)
                        key = text if adv <= w else (text, w)
                        st = cache.get(key)
                        if st is None:
                            if len(cache) > 8000:
                                cache.clear()
                            shown = text if adv <= w else fmx.elidedText(text[:w // 3 + 2], ELIDE, w)
                            st = G.QStaticText(shown)
                            st.setTextFormat(PLAIN)
                            st.setPerformanceHint(G.QStaticText.AggressiveCaching)
                            st.prepare(s.ident, fnt)
                            cache[key] = st
                        p.drawStaticText(rect.left() + 8, y, st)
                    y += s.lh

        if opt.state & SELECTED:
            p.setPen(s.SEL)
            p.drawLine(rect.left(), rect.top() + 1, rect.right(), rect.top() + 1)
            p.drawLine(rect.left(), rect.bottom() - 1, rect.right(), rect.bottom() - 1)
            if c == 0:
                p.drawLine(rect.left() + 1, rect.top(), rect.left() + 1, rect.bottom())
            if c == m.columnCount() - 1:
                p.drawLine(rect.right() - 1, rect.top(), rect.right() - 1, rect.bottom())


HDR_QSS = Template("QHeaderView::section { background: $hdr_bg; color: $hdr_fg; font-size: 16px; font-weight: 600; "
                   "padding: 0 8px; border: none; border-right: 1px solid $line; border-bottom: 1px solid $line; }")
SB_QSS = Template("QScrollBar:vertical { width: 10px; background: transparent; }"
          "QScrollBar:horizontal { height: 10px; background: transparent; }"
          "QScrollBar::handle { background: $handle; border-radius: 4px; min-height: 30px; min-width: 30px; }"
          "QScrollBar::handle:hover { background: $handle_hover; }"
          "QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; }"
          "QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }")


class Tab(W.QTableView):
    tmpdir = None                                    # set for a new, never-saved sheet (its scratch folder)

    def __init__(s, path, notes=False):
        super().__init__()
        s.data_ = CsvData(path, notes)
        s.m = Model(s.data_)
        s.setModel(s.m)
        s.setItemDelegate(Delegate(s, s.m))
        s.setSelectionBehavior(W.QAbstractItemView.SelectRows)
        s.setSelectionMode(W.QAbstractItemView.ExtendedSelection)
        s.setEditTriggers(W.QAbstractItemView.DoubleClicked | W.QAbstractItemView.EditKeyPressed)
        s.setShowGrid(False); s.setWordWrap(False); s.setFrameShape(W.QFrame.NoFrame)
        s.setCornerButtonEnabled(False)
        s.setVerticalScrollMode(W.QAbstractItemView.ScrollPerPixel)
        s.setHorizontalScrollMode(W.QAbstractItemView.ScrollPerPixel)
        s.verticalScrollBar().setSingleStep(ROW_H)
        s.setContextMenuPolicy(C.Qt.CustomContextMenu)
        vh = s.verticalHeader(); vh.hide()
        vh.setSectionResizeMode(W.QHeaderView.Fixed); vh.setDefaultSectionSize(ROW_H)
        hh = s.horizontalHeader()
        hh.setHighlightSections(False); hh.setSectionsMovable(True)
        hh.setDefaultAlignment(C.Qt.AlignLeft | C.Qt.AlignVCenter); hh.setFixedHeight(30)
        fm = s.fontMetrics()
        s.setColumnWidth(0, fm.horizontalAdvance(str(s.data_.n)) + 28)
        for c in range(len(s.data_.cols)):
            w = fm.horizontalAdvance(s.data_.cols[c]) + 24
            for r in range(min(100, s.data_.n)):
                w = max(w, fm.horizontalAdvance(s.data_.field(r, c)[:80]) + 22)
            s.setColumnWidth(c + 1, max(60, min(w, 420)))
        s._fp = False                                    # multi-line: rows grow only when they hold line breaks
        sb = s.verticalScrollBar()
        sb.valueChanged.connect(s.sched)
        for sig in (s.m.modelReset, s.m.layoutChanged, s.m.dataChanged, s.m.rowsInserted, s.m.rowsRemoved):
            sig.connect(s.sched)
        for sig in (s.m.headerDataChanged, s.m.columnsInserted, s.m.edited):
            sig.connect(s.fit_header)
        s.fit_header(); s.sched()
        s.customContextMenuRequested.connect(s.menu)
        hh.setContextMenuPolicy(C.Qt.CustomContextMenu); hh.customContextMenuRequested.connect(s.hmenu)
        hh.sectionDoubleClicked.connect(lambda i: i and s.rename_col(i - 1))
        s._anim = C.QVariantAnimation(s); s._anim.setDuration(140)
        s._anim.setEasingCurve(C.QEasingCurve.OutCubic)
        s._anim.valueChanged.connect(lambda v: s.verticalScrollBar().setValue(int(v)))
        s._target = 0
        s.restyle()

    def resizeEvent(s, e):
        super().resizeEvent(e); s.sched()

    def sched(s, *_):
        if not s._fp:
            s._fp = True; C.QTimer.singleShot(0, s.fit_rows)

    def fit_header(s, *_):
        d, n = s.data_, 1
        for c, name in enumerate(d.cols):
            if '\n' in name and c not in d.dcols:
                n = max(n, name.count('\n') + 1)
        f = G.QFont(s.horizontalHeader().font()); f.setPixelSize(16); f.setWeight(G.QFont.DemiBold)
        s.horizontalHeader().setFixedHeight(max(30, n * G.QFontMetrics(f).lineSpacing() + 10))

    def fit_rows(s):
        """Rows are fixed height, except rows holding a line break: only the VISIBLE rows are checked
        (a byte search on the raw line), so cost does not depend on file size."""
        s._fp = False
        m, d, dl = s.m, s.data_, s.itemDelegate()
        n = m.rowCount()
        if not n:
            return
        ed = {k[0] for k in d.edits} if d.edits else ()
        ncol, lh, changed = len(d.cols), dl.lh, False
        r = max(s.rowAt(0), 0)
        y, H = s.rowViewportPosition(r), s.viewport().height()
        while r < n and y < H:
            rid = m.src(r)
            lines = 1
            if rid >= d.ns or rid in ed:
                for c in range(ncol):
                    v = d.field(rid, c)
                    if '\n' in v:
                        lines = max(lines, v.count('\n') + 1)
            elif b'\n' in d.raw(rid + 1).rstrip(b'\r\n'):
                for v in d.row(rid):
                    if '\n' in v:
                        lines = max(lines, v.count('\n') + 1)
            h = ROW_H + (min(lines, dl.MAXL) - 1) * lh
            if s.rowHeight(r) != h:
                s.setRowHeight(r, h); changed = True
            y += h; r += 1
        if changed:
            s.viewport().update(); s.sched()

    def wheelEvent(s, e):
        sb = s.verticalScrollBar()
        pd, ad = e.pixelDelta(), e.angleDelta()
        if e.modifiers() & C.Qt.ShiftModifier:        # Shift + wheel -> horizontal
            hb = s.horizontalScrollBar()
            hb.setValue(hb.value() - ((pd.y() or pd.x()) if not pd.isNull() else (ad.y() or ad.x())))
            e.accept(); return
        if not pd.isNull():                          # trackpad: the OS already sends smooth pixel deltas
            sb.setValue(sb.value() - pd.y())
            hb = s.horizontalScrollBar(); hb.setValue(hb.value() - pd.x())
            e.accept(); return
        if ad.y() == 0:
            super().wheelEvent(e); return
        running = s._anim.state() == C.QAbstractAnimation.Running
        base = s._target if running else sb.value()
        s._target = max(sb.minimum(), min(sb.maximum(), base - ad.y() / 120 * ROW_H * 3))
        s._anim.stop(); s._anim.setStartValue(sb.value()); s._anim.setEndValue(int(s._target)); s._anim.start()
        e.accept()

    def restyle(s):
        # styled per-widget (NOT app-wide) so the big table stays on the fast path
        s.horizontalHeader().setStyleSheet(HDR_QSS.substitute(CUR))
        sb = SB_QSS.substitute(CUR)
        s.verticalScrollBar().setStyleSheet(sb); s.horizontalScrollBar().setStyleSheet(sb)
        s.itemDelegate().restyle()
        bg = G.QColor(CUR['bg'])                          # empty area below/right of the rows and the little square
        tp = s.palette()                                  # where the two scrollbars meet: set them explicitly,
        tp.setColor(G.QPalette.Window, bg); tp.setColor(G.QPalette.Base, bg)   # don't rely on the app palette
        s.setPalette(tp)                                  # having propagated
        vp = s.viewport()
        pal = vp.palette()
        pal.setColor(vp.backgroundRole(), bg)
        vp.setPalette(pal)
        vp.update()

    def sel_ranges(s):
        return [(r.top(), r.bottom()) for r in s.selectionModel().selection()]

    def sel_rows(s):
        for t, b in s.sel_ranges():
            yield from s.m.src_range(t, b)

    def sel_count(s): return sum(b - t + 1 for t, b in s.sel_ranges())

    def mark(s, color): s.m.mark(list(s.sel_rows()), color)

    def cmark(s, color, c=None):                     # colour the current column in all selected rows
        c = s.currentIndex().column() - 1 if c is None else c
        if c >= 0:
            s.m.cmark(list(s.sel_rows()), c, color)

    def copy_col(s):
        c = s.currentIndex().column() - 1
        d = s.data_
        txt = '\n'.join(d.field(r, c) if c >= 0 else d.line(r + 1) if r < d.ns else d.d.join(
            d.field(r, k) for k in range(len(d.cols))) for r in s.sel_rows())
        if txt:
            W.QApplication.clipboard().setText(txt)

    def menu(s, pos):
        mn = W.QMenu(s)
        for k in TINT:
            mn.addAction(k.capitalize(), lambda k=k: s.mark(k))
        mn.addAction('Clear mark', lambda: s.mark(None)); mn.addSeparator()
        cc = s.indexAt(pos).column() - 1
        if cc >= 0:
            sub = mn.addMenu('Color cell')
            for k in TINT:
                sub.addAction(k.capitalize(), lambda k=k: s.cmark(k, cc))
            sub.addAction('Clear cell color', lambda: s.cmark(None, cc)); mn.addSeparator()
        mn.addAction('Copy column values', s.copy_col); mn.addSeparator()
        mn.addAction('Insert row above', lambda: s.insert_row(True))
        mn.addAction('Insert row below', lambda: s.insert_row(False))
        mn.addAction('Delete row(s)', s.delete_rows)
        c = s.indexAt(pos).column() - 1
        if c >= 0:
            mn.addSeparator(); s.col_actions(mn, c)
        mn.exec(s.viewport().mapToGlobal(pos))

    def hmenu(s, pos):
        hh = s.horizontalHeader(); i = hh.logicalIndexAt(pos)
        if i > 0:
            mn = W.QMenu(s); s.col_actions(mn, i - 1)
            mn.exec(hh.mapToGlobal(pos))

    def col_actions(s, mn, c):
        mn.addAction('Insert column left', lambda: s.add_col(c, False))
        mn.addAction('Insert column right', lambda: s.add_col(c, True))
        mn.addAction('Rename column', lambda: s.rename_col(c))
        mn.addAction('Delete column', lambda: s.delete_col(c))

    # ---- add / delete rows and columns
    def keyPressEvent(s, e):
        if s.state() != W.QAbstractItemView.EditingState:
            if e.key() == C.Qt.Key_Delete:
                s.delete_rows(); return
            if e.key() == C.Qt.Key_Insert:
                s.insert_row(bool(e.modifiers() & C.Qt.ShiftModifier)); return
        super().keyPressEvent(e)

    def insert_row(s, above=False):
        m, ix = s.m, s.currentIndex()
        k = m.insert_row(ix.row() if ix.isValid() else m.rowCount() - 1, not above)
        s.setCurrentIndex(m.index(k, max(ix.column(), 1))); s.scrollTo(s.currentIndex())

    def delete_rows(s):
        rg = s.sel_ranges()
        if not rg:
            return
        pos = np.unique(np.concatenate([np.arange(t, b + 1, dtype=np.int64) for t, b in rg]))
        if len(pos) > 1 and W.QMessageBox.question(s, 'ReconSheet', 'Delete %s rows?' % '{:,}'.format(len(pos)),
                                                   W.QMessageBox.Yes | W.QMessageBox.No, W.QMessageBox.No) != W.QMessageBox.Yes:
            return
        s.m.delete_rows(pos)
        n = s.m.rowCount()
        if n:
            s.selectRow(min(int(pos[0]), n - 1))

    def add_col(s, ref=None, right=True):
        if ref is None:                                   # toolbar: right of the current column
            c = s.currentIndex().column() - 1
            ref = c if c >= 0 else None
        name, ok = ask_text(s, 'ReconSheet', 'New column name:')
        name = name.strip()
        if not ok or not name:
            return
        c = s.m.add_col(name, ref, right)
        hh = s.horizontalHeader()
        if ref is not None:
            hh.moveSection(hh.visualIndex(c + 1), hh.visualIndex(ref + 1) + (1 if right else 0))
        s.setColumnWidth(c + 1, max(120, s.fontMetrics().horizontalAdvance(name) + 40))
        if s.m.rowCount():
            ix = s.m.index(max(s.currentIndex().row(), 0), c + 1)
            s.selectionModel().setCurrentIndex(ix, C.QItemSelectionModel.NoUpdate); s.scrollTo(ix)

    def rename_col(s, c):
        name, ok = ask_text(s, 'ReconSheet', 'Column name:', s.data_.cols[c])
        if ok and name.strip() and name.strip() != s.data_.cols[c]:
            s.m.rename_col(c, name.strip())

    def delete_col(s, c):
        d = s.data_
        if len(d.cols) - len(d.dcols) <= 1:
            W.QMessageBox.information(s, 'ReconSheet', 'A CSV needs at least one column.'); return
        if W.QMessageBox.question(s, 'ReconSheet', 'Delete column "%s"?' % d.cols[c],
                                  W.QMessageBox.Yes | W.QMessageBox.No, W.QMessageBox.No) == W.QMessageBox.Yes:
            s.m.delete_col(c); s.setColumnHidden(c + 1, True)

    # ---- undo / redo (Ctrl+Z / Ctrl+Shift+Z)
    def undo(s): s._step(s.m.undo, s.m.redo, True)
    def redo(s): s._step(s.m.redo, s.m.undo, False)

    def _step(s, src, dst, undo):
        m = s.m
        if not src or s.state() == W.QAbstractItemView.EditingState:      # while editing, the cell editor owns Ctrl+Z
            return
        rec = src.pop()
        m.replaying = True
        try:
            focus = s._apply(rec, undo)
        except Exception:
            m.undo.clear(); m.redo.clear()                                # history can't be trusted any more
            raise
        finally:
            m.replaying = False
        dst.append(rec)
        if focus:
            if focus[0] == 'cell':
                ix = m.index(focus[1], focus[2]); s.setCurrentIndex(ix); s.scrollTo(ix)
            elif focus[0] == 'sel':
                s.select_ids(focus[1])
            elif focus[0] == 'near' and focus[1] is not None and m.rowCount():
                s.selectRow(min(int(focus[1]), m.rowCount() - 1))
        m.edited.emit()

    def select_ids(s, ids):
        m = s.m
        p = m.vposs(ids)
        if not len(p):
            return
        runs = np.split(p, np.flatnonzero(np.diff(p) > 1) + 1)
        first = m.index(int(p[0]), 1)
        if len(runs) <= 500:
            sel, last = C.QItemSelection(), m.columnCount() - 1
            for r in runs:
                sel.select(m.index(int(r[0]), 0), m.index(int(r[-1]), last))
            s.selectionModel().setCurrentIndex(first, C.QItemSelectionModel.NoUpdate)
            s.selectionModel().select(sel, C.QItemSelectionModel.ClearAndSelect)
        s.scrollTo(first)

    def _apply(s, rec, undo):
        m = s.m; d = m.d; k = rec[0]
        if k == 'cell':
            _, r, c, prev, new = rec
            v = prev if undo else new
            if v is None:
                d.edits.pop((r, c), None)
            else:
                d.edits[(r, c)] = v
            m.ranks.pop(c, None)
            p = m.vpos1(r)
            if p is None:
                return None
            ix = m.index(p, c + 1); m.dataChanged.emit(ix, ix)
            return ('cell', p, c + 1)
        if k == 'ins':
            rid, at = rec[1], rec[2]
            ids = np.array([rid], np.int64)
            if undo:
                rec[4] = {kk: vv for kk, vv in d.edits.items() if kk[0] == rid}
                p0 = m.vpos1(rid)
                m.hide(ids)
                d.cols_dirty, d.struct = rec[3]
                return ('near', p0)
            m.restore(ids, np.array([at], np.int64), rec[4] or {})
            return ('sel', ids)
        if k == 'del':
            _, ids, at, ed, fl = rec
            if undo:
                m.restore(ids, at, ed)
                d.cols_dirty, d.struct = fl
                return ('sel', ids)
            p0 = m.vposs(ids)
            m.hide(ids)
            return ('near', int(p0[0]) if len(p0) else None)
        if k == 'ren':
            _, c, old, new, fl = rec
            m.rename_col(c, old if undo else new)
            if undo:
                d.cols_dirty, d.struct = fl
            return None
        if k == 'delcol':
            _, c, fl = rec
            if undo:
                d.dcols.discard(c); d.cols_dirty, d.struct = fl
                s.setColumnHidden(c + 1, False)
            else:
                m.delete_col(c); s.setColumnHidden(c + 1, True)
            return None
        if k == 'addcol':
            _, c, name, ref, right, fl = rec[:6]
            hh = s.horizontalHeader()
            if undo:
                rec[6], rec[7] = s.columnWidth(c + 1), hh.visualIndex(c + 1)      # remember the look for redo
                m.drop_col(c)
                d.cols_dirty, d.struct = fl
            else:
                m.add_col(name, ref, right)
                s.setColumnWidth(c + 1, rec[6])
                hh.moveSection(hh.visualIndex(c + 1), rec[7])
            return None
        return None

    def refont(s):
        """The app font changed: re-read it for the painted cells (the delegate caches its fonts)."""
        s.itemDelegate().refont(s.font())
        s.sched()
        s.horizontalHeader().setFont(s.font())        # column names (the header has its own stylesheet)
        s.viewport().update()

    def dirty(s): return s.data_.changed()

    def write(s):
        d, m, hh = s.data_, s.m, s.horizontalHeader()
        rows_ch, cols_ch, marks = d.nat is not None, d.cols_dirty, m.marks
        if rows_ch:                                       # file row numbers change -> carry marks over
            marks = {int(d.pos[r]): k for r, k in m.marks.items() if d.pos[r] >= 0}
        cm = m.cmarks
        if rows_ch or cols_ch:
            kc = {c: i for i, c in enumerate(c for c in d.corder if c not in d.dcols)}
            cm = {(int(d.pos[r]) if rows_ch else r, kc[c] if cols_ch else c): k for (r, c), k in m.cmarks.items()
                  if (not rows_ch or d.pos[r] >= 0) and (not cols_ch or c in kc)}
        if cols_ch:                                       # remember widths / hidden / visual order (old column ids)
            keep = [c for c in d.corder if c not in d.dcols]
            look = {c: (s.columnWidth(c + 1), s.isColumnHidden(c + 1)) for c in keep}
            vis = [hh.logicalIndex(v) - 1 for v in range(hh.count())]
        m.beginResetModel(); d.save(); m.ranks.clear()
        m.undo.clear(); m.redo.clear()                    # a save renumbers rows -> old history no longer applies
        if rows_ch or cols_ch:
            m.marks, m.cmarks, m.view, m.matches = marks, cm, None, None
        if cols_ch:
            m.sortc = None; m.retag()
        m.endResetModel()
        if cols_ch:
            new = {c: i + 1 for i, c in enumerate(keep)}
            for c, (w, hid) in look.items():
                s.setColumnWidth(new[c], w); s.setColumnHidden(new[c], hid)
            for v, lg in enumerate([0 if c < 0 else new[c] for c in vis if c < 0 or c in new]):
                hh.moveSection(hh.visualIndex(lg), v)
        if rows_ch or cols_ch:
            m.search(m.query) if m.query else m.rebuild()
        m.save_marks(); m.shape.emit(); m.edited.emit()


# ---------------------------------------------------------------- UI
def icon(path):
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="%s" '
           'stroke-width="2" stroke-linecap="round" stroke-linejoin="round">%s</svg>' % (CUR['icon'], path))
    pm = G.QPixmap(36, 36); pm.fill(C.Qt.transparent)
    p = G.QPainter(pm); QSvgRenderer(svg.encode()).render(p); p.end()
    return G.QIcon(pm)


# Stylesheets are applied ONLY to the toolbar / tab bar / header / scrollbars.
# An app-wide "* {...}" sheet forces every widget (including the big table) through
# QStyleSheetStyle, which is the main reason scrolling felt heavy.
BAR_QSS = Template("""
QToolButton { border: none; border-radius: 6px; padding: 5px; background: transparent; }
QToolButton:hover { background: $hover; }
QToolButton:checked { background: $checked; }
QToolButton#dot { border-radius: 7px; min-width: 14px; max-width: 14px; min-height: 14px; max-height: 14px; padding: 0; margin: 0 3px; }
QToolButton#dot:hover { border: 2px solid $dot_hover; }
QFrame#sep { background: $line; max-width: 1px; min-width: 1px; }
QLineEdit#search { background: $field; border: 1px solid $line; border-radius: 7px; padding: 3px 10px; min-width: 420px; color: $text; }
QLineEdit#search:focus { border: 1px solid #3B82F6; background: $bg; }
""")
TAB_QSS = Template("""
QTabBar::tab { padding: 6px 14px; color: $muted; border: none; background: $bg; }
QTabBar::tab:selected { color: $text; border-bottom: 2px solid #3B82F6; }
""")
STATUS_QSS = Template("color: $muted; font-size: 14px;")


def rss_mb():
    try:
        return int(open('/proc/self/statm').read().split()[1]) * os.sysconf('SC_PAGE_SIZE') >> 20
    except Exception:
        return 0


class Main(W.QMainWindow):
    def __init__(s, files):
        super().__init__()
        s.setWindowTitle('ReconSheet'); s.resize(1100, 640); s.setAcceptDrops(True)
        root = W.QWidget(); s.setCentralWidget(root)
        v = W.QVBoxLayout(root); v.setContentsMargins(0, 0, 0, 0); v.setSpacing(0)

        s.icons = []
        s.bar = bar = W.QWidget(); bar.setObjectName('bar'); bar.setStyleSheet(BAR_QSS.substitute(CUR))
        h = W.QHBoxLayout(bar); h.setContentsMargins(14, 10, 14, 10); h.setSpacing(4)

        def btn(path, tip, fn, check=False):
            b = W.QToolButton(); b.setIcon(icon(path)); b.setIconSize(C.QSize(18, 18))
            b.setToolTip(tip); b.setCheckable(check); b.clicked.connect(fn); h.addWidget(b)
            s.icons.append((b, path))
            return b

        def sep():
            f = W.QFrame(); f.setObjectName('sep'); f.setFixedHeight(18)
            h.addSpacing(6); h.addWidget(f); h.addSpacing(6)

        btn('<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5M12 12v6M9 15h6"/>',
            'New file (Ctrl+N)', s.new_file)
        btn('<path d="M3 7v12h18V9H11L9 7z"/>', 'Open (Ctrl+O)', s.open_dialog)
        btn('<rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V6a2 2 0 0 1 2-2h9"/>',
            'Copy selected rows of current column (Ctrl+C)', lambda: s.cur() and s.cur().copy_col())
        sep()
        btn('<rect x="3" y="4" width="18" height="6" rx="1"/><path d="M12 14v6M9 17h6"/>',
            'Insert row below (Ctrl+Shift+R / Ins) - Shift+Ins inserts above', lambda: s.cur() and s.cur().insert_row(False))
        btn('<rect x="4" y="3" width="6" height="18" rx="1"/><path d="M17 9v6M14 12h6"/>',
            'Insert column right of current column (Ctrl+Shift+C)', lambda: s.cur() and s.cur().add_col())
        btn('<path d="M4 7h16M10 11v6M14 11v6M6 7l1 12h10l1-12M9 7V4h6v3"/>',
            'Delete selected rows (Del)', lambda: s.cur() and s.cur().delete_rows())
        sep()
        btn('<circle cx="11" cy="11" r="6"/><path d="M20 20l-4.5-4.5"/>', 'Search (Ctrl+F)', lambda: s.search.setFocus())
        s.dark_btn = btn(SUN if CUR['name'] == 'dark' else MOON, 'Dark / light mode (Ctrl+D)', s.toggle_dark)
        btn('<path d="M9 4v16M15 4v16"/>', 'Show / hide columns', s.col_menu)
        sep()
        s.only = btn('<path d="M12 3l9 9-9 9-9-9z"/>', 'Show only marked rows', s.toggle_marked, True)
        sep()
        for k in TINT:
            d = W.QToolButton(); d.setObjectName('dot'); d.setToolTip('Mark selected rows ' + k)
            d.setStyleSheet('QToolButton#dot{background:%s}' % INK[k])
            d.clicked.connect(lambda _=0, k=k: s.cur() and s.cur().mark(k)); h.addWidget(d)
        sep()
        btn('<path d="M5 15l7-7 7 7"/>', 'Sort by current column (asc → desc → off)', s.sort)
        btn('<circle cx=\"12\" cy=\"12\" r=\"3\"/><path d=\"M19.14 10.05L21.89 10.51L21.89 13.49L19.14 13.95L18.43 15.67L20.04 17.94L17.94 20.04L15.67 18.43L13.95 19.14L13.49 21.89L10.51 21.89L10.05 19.14L8.33 18.43L6.06 20.04L3.96 17.94L5.57 15.67L4.86 13.95L2.11 13.49L2.11 10.51L4.86 10.05L5.57 8.33L3.96 6.06L6.06 3.96L8.33 5.57L10.05 4.86L10.51 2.11L13.49 2.11L13.95 4.86L15.67 5.57L17.94 3.96L20.04 6.06L18.43 8.33z\"/>', 'Settings', s.settings_menu)
        h.addStretch(1)
        s.search = W.QLineEdit(); s.search.setObjectName('search'); s.search.setPlaceholderText('Search')
        s.search.setClearButtonEnabled(True); h.addWidget(s.search)
        v.addWidget(bar)

        s.tabs = W.QTabWidget(); s.tabs.setDocumentMode(True); s.tabs.setTabsClosable(True)
        s.tabs.setMovable(True); s.tabs.tabBar().setAutoHide(True); s.tabs.tabBar().setStyleSheet(TAB_QSS.substitute(CUR))
        s.tabs.tabCloseRequested.connect(s.close_tab); s.tabs.currentChanged.connect(s.on_tab)
        v.addWidget(s.tabs, 1)

        foot = W.QWidget(); foot.setObjectName('foot')
        fh = W.QHBoxLayout(foot); fh.setContentsMargins(14, 7, 24, 7)
        s.status, s.fname = W.QLabel(), W.QLabel()
        s.status.setStyleSheet(STATUS_QSS.substitute(CUR)); s.fname.setStyleSheet(STATUS_QSS.substitute(CUR))
        fh.addWidget(s.status); fh.addStretch(1); fh.addWidget(s.fname)
        v.addWidget(foot)

        s.timer = C.QTimer(s); s.timer.setSingleShot(True); s.timer.timeout.connect(s.run_search)
        s.search.textChanged.connect(lambda _: s.timer.start(350))
        s.tick = C.QTimer(s); s.tick.timeout.connect(s.refresh); s.tick.start(3000)

        # selection changes can fire many times per second while dragging -> coalesce the status update
        s.rt = C.QTimer(s); s.rt.setSingleShot(True); s.rt.timeout.connect(s.refresh)

        sc = lambda key, fn: G.QShortcut(G.QKeySequence(key), s, fn)
        sc('Ctrl+N', s.new_file); sc('Ctrl+O', s.open_dialog); sc('Ctrl+S', s.save); sc('Ctrl+F', lambda: (s.search.setFocus(), s.search.selectAll()))
        sc('Ctrl+W', lambda: s.close_tab(s.tabs.currentIndex())); sc('Ctrl+C', lambda: s.cur() and s.cur().copy_col())
        for i, k in enumerate(TINT, 1):
            sc('Ctrl+%d' % i, lambda k=k: s.cur() and s.cur().mark(k))
        sc('Ctrl+0', lambda: s.cur() and s.cur().mark(None))
        for i, k in enumerate(TINT, 1):                 # Alt+1..4 colour the current cell(s), Alt+0 clears
            sc('Alt+%d' % i, lambda k=k: s.cur() and s.cur().cmark(k))
        sc('Alt+0', lambda: s.cur() and s.cur().cmark(None))
        sc('Ctrl+Shift+R', lambda: s.cur() and s.cur().insert_row(False))
        sc('Ctrl+Shift+C', lambda: s.cur() and s.cur().add_col())
        sc('Ctrl+D', s.toggle_dark)
        sc('Ctrl+Z', lambda: s.cur() and s.cur().undo())
        sc('Ctrl+Shift+Z', lambda: s.cur() and s.cur().redo())
        s.default_font = G.QFont(W.QApplication.font())      # the font main() set up: fallback for Settings > Font
        saved = get_setting('font', '')
        if saved and isinstance(saved, str):
            s.apply_font(saved)
        cp = None
        if not files:                                      # started without files: reopen what was open last time
            sess = get_setting('session', {})
            sess = sess if isinstance(sess, dict) else {}
            last, cp = sess.get('files', []), sess.get('current')
            files = [p for p in last if isinstance(p, str) and os.path.isfile(p)] if isinstance(last, list) else []
        s.open_files(files)
        for i in range(s.tabs.count()):                    # back to the tab that was active
            if cp and s.tabs.widget(i).data_.path == cp:
                s.tabs.setCurrentIndex(i); break
        s.refresh()

    def cur(s): return s.tabs.currentWidget()

    def restyle(s):
        s.bar.setStyleSheet(BAR_QSS.substitute(CUR))
        s.tabs.tabBar().setStyleSheet(TAB_QSS.substitute(CUR))
        q = STATUS_QSS.substitute(CUR)
        s.status.setStyleSheet(q); s.fname.setStyleSheet(q)
        for i, (b, path) in enumerate(s.icons):
            if b is s.dark_btn:
                path = SUN if CUR['name'] == 'dark' else MOON
                s.icons[i] = (b, path)
            b.setIcon(icon(path))
        for i in range(s.tabs.count()):
            s.tabs.widget(i).restyle()

    def toggle_dark(s):
        name = 'light' if CUR['name'] == 'dark' else 'dark'
        apply_theme(name)
        set_setting('theme', name)
        s.restyle()

    # ---- settings
    def settings_menu(s):
        mn = W.QMenu(s)
        mn.addAction('Font settings...', s.font_dialog)
        mn.exec(G.QCursor.pos())

    def apply_font(s, name):
        """Use the installed font `name` for the whole UI (the default font stays as fallback).
        Returns the real family name, or None (-> the default font is used)."""
        fams = {f.lower(): f for f in G.QFontDatabase.families()}
        real = fams.get(name.strip().lower())
        f = G.QFont(s.default_font)
        if real:
            f.setFamilies([real] + [x for x in s.default_font.families() if x != real])
        W.QApplication.setFont(f)
        for w in (s.bar, s.tabs.tabBar(), s.status, s.fname):   # styled widgets don't pick up the app font by themselves
            w.setFont(f)
        for i in range(s.tabs.count()):
            s.tabs.widget(i).refont()
        return real

    def font_dialog(s):
        name, ok = W.QInputDialog.getText(s, 'ReconSheet', 'Font name:', text=W.QApplication.font().family())
        if not ok:
            return
        name = name.strip()
        real = s.apply_font(name)
        if real:
            set_setting('font', real)
        else:
            set_setting('font', None)
            if name:
                W.QMessageBox.information(s, 'ReconSheet', 'Font "%s" was not found on this system.\nUsing the default font.' % name)

    def lazy_refresh(s):
        if not s.rt.isActive():
            s.rt.start(60)

    def open_files(s, paths, notes=False):
        for p in paths:
            try:
                t = Tab(p, notes)
            except Exception as e:
                W.QMessageBox.warning(s, 'ReconSheet', 'Cannot open %s\n%s' % (p, e)); continue
            t.m.shape.connect(s.refresh); t.m.edited.connect(s.refresh)
            t.selectionModel().selectionChanged.connect(lambda *_: s.lazy_refresh())
            i = s.tabs.addTab(t, os.path.basename(p)); s.tabs.setTabToolTip(i, p)
            s.tabs.setCurrentIndex(i)

    # ---- new (from scratch) sheets: a scratch file in a temp folder until the first save
    NEW_COLS, NEW_ROWS = 'A,B,C,D,E', 30

    def new_file(s):
        s.nuntitled = getattr(s, 'nuntitled', 0) + 1
        tmp = tempfile.mkdtemp(prefix='reconsheet_')
        p = os.path.join(tmp, 'Untitled-%d.csv' % s.nuntitled)
        try:
            with open(p, 'w', newline='') as f:
                f.write(s.NEW_COLS + '\n' + (',' * s.NEW_COLS.count(',') + '\n') * s.NEW_ROWS)
        except OSError as e:
            shutil.rmtree(tmp, ignore_errors=True)
            W.QMessageBox.warning(s, 'ReconSheet', 'Cannot create a new file\n%s' % e); return
        s.open_files([p], notes=False)
        t = s.cur()
        if t is None or t.data_.path != p:                 # Tab() failed (already reported)
            shutil.rmtree(tmp, ignore_errors=True); return
        t.tmpdir = tmp
        s.tabs.setTabToolTip(s.tabs.currentIndex(), 'Not saved yet')
        t.setCurrentIndex(t.m.index(0, 1)); t.setFocus()

    def trim_blank(s, t):
        """Before the first save: drop completely empty rows from the end (the spare rows of a new sheet)."""
        d, m = t.data_, t.m
        ids = np.arange(d.n, dtype=np.int64) if d.nat is None else d.nat
        keep_cols = [c for c in d.corder if c not in d.dcols]
        cut = []
        for r in ids[::-1].tolist():
            if r in m.marks or any(k[0] == r for k in m.cmarks) or any(d.field(r, c) for c in keep_cols):
                break
            cut.append(r)
        if cut:
            d.delete_rows(cut)

    def save_new(s, t):
        """Save As for a sheet that has no file yet. Returns False if the user cancelled / it failed."""
        d, m = t.data_, t.m
        p, _ = W.QFileDialog.getSaveFileName(s, 'Save CSV', os.path.basename(d.path),
                                             'CSV (*.csv);;All files (*)')
        if not p:
            return False
        if not os.path.splitext(p)[1]:
            p += '.csv'
        old = (d.path, m.mpath, m.cpath)
        d.path = p; m.mpath, m.cpath = colormap_paths(p)
        try:
            s.busy(lambda: (s.trim_blank(t), t.write()))
        except Exception as e:
            d.path, m.mpath, m.cpath = old
            m.undo.clear(); m.redo.clear()
            W.QMessageBox.warning(s, 'ReconSheet', 'Cannot save %s\n%s' % (p, e)); return False
        shutil.rmtree(t.tmpdir, ignore_errors=True); t.tmpdir = None
        s.tabs.setTabToolTip(s.tabs.indexOf(t), p)
        s.refresh()
        return True

    def write_tab(s, t):
        if t.tmpdir:
            return s.save_new(t)
        t.write(); return True

    def open_dialog(s):
        ps, _ = W.QFileDialog.getOpenFileNames(s, 'Open CSV', '', 'CSV / text (*.csv *.tsv *.txt);;All files (*)')
        s.open_files(ps)

    def dragEnterEvent(s, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(s, e):
        s.open_files([u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()])

    def busy(s, fn):
        W.QApplication.setOverrideCursor(C.Qt.WaitCursor)
        W.QApplication.processEvents()
        try:
            fn()
        finally:
            W.QApplication.restoreOverrideCursor()

    def save(s):
        t = s.cur()
        if t and t.tmpdir:
            s.save_new(t)
        elif t and t.dirty():
            s.busy(t.write)
        elif t:
            t.m.save_marks()

    def close_tab(s, i):
        t = s.tabs.widget(i)
        if t is None:
            return True
        if t.dirty():
            a = W.QMessageBox.question(s, 'ReconSheet', 'Save changes to %s?' % os.path.basename(t.data_.path),
                                       W.QMessageBox.Save | W.QMessageBox.Discard | W.QMessageBox.Cancel)
            if a == W.QMessageBox.Cancel:
                return False
            if a == W.QMessageBox.Save and not s.write_tab(t):
                return False
        if not t.tmpdir:                                  # a never-saved sheet has no file to attach colours to
            t.m.save_marks()
        s.tabs.removeTab(i); t.data_.close(); t.deleteLater()
        if t.tmpdir:
            shutil.rmtree(t.tmpdir, ignore_errors=True)
        return True

    def closeEvent(s, e):
        paths = [s.tabs.widget(i).data_.path for i in range(s.tabs.count()) if not s.tabs.widget(i).tmpdir]
        t = s.cur()
        cur = t.data_.path if t is not None and not t.tmpdir else None     # never-saved sheets can't be reopened
        while s.tabs.count():
            if not s.close_tab(0):
                e.ignore(); return
        set_setting('session', {'files': paths, 'current': cur})            # only once every tab closed for real
        e.accept()

    def on_tab(s, _=0):
        t = s.cur()
        s.search.blockSignals(True); s.search.setText(t.m.query if t else ''); s.search.blockSignals(False)
        s.only.setChecked(bool(t and t.m.marked_only)); s.refresh()

    def run_search(s):
        t = s.cur()
        if t and t.m.query != s.search.text():
            s.busy(lambda: t.m.search(s.search.text()))

    def toggle_marked(s):
        t = s.cur()
        if t:
            t.m.marked_only = s.only.isChecked(); t.m.rebuild()
        else:
            s.only.setChecked(False)

    def sort(s):
        t = s.cur(); c = t.currentIndex().column() - 1 if t else -1
        if c < 0:
            return
        m = t.m
        if m.sortc != c:
            m.sortc, m.asc = c, True
        elif m.asc:
            m.asc = False
        else:
            m.sortc = None
        s.busy(m.rebuild)

    def col_menu(s):
        t = s.cur()
        if not t:
            return
        mn = W.QMenu(s)
        for i, name in enumerate(t.data_.cols):
            if i in t.data_.dcols:
                continue
            a = mn.addAction(name); a.setCheckable(True); a.setChecked(not t.isColumnHidden(i + 1))
            a.toggled.connect(lambda on, i=i: t.setColumnHidden(i + 1, not on))
        mn.exec(G.QCursor.pos())

    def refresh(s):
        t = s.cur()
        for i in range(s.tabs.count()):
            w = s.tabs.widget(i)
            s.tabs.setTabText(i, os.path.basename(w.data_.path) + (' •' if w.dirty() else ''))
        if t:
            shown, total = t.m.rowCount(), t.data_.live
            rows = '{:,}'.format(shown) + (' / {:,}'.format(total) if shown != total else '')
            s.fname.setText(os.path.basename(t.data_.path) + (' *' if t.dirty() else ''))
        else:
            rows = '0'; s.fname.setText('')
        s.status.setText('Rows: %s  |  Selected: %d  |  Mem: %dMB' % (rows, t.sel_count() if t else 0, rss_mb()))


def main():
    app = W.QApplication(sys.argv)
    app.setStyle('Fusion')                      # light, fast, identical on every OS
    #f = app.font(); f.setPixelSize(18); app.setFont(f)
    f = app.font(); f.setFamilies(['Inter', 'Menlo', 'Consolas', 'DejaVu Sans Mono']); f.setPixelSize(18); app.setFont(f)
    saved = get_setting('theme', 'light')
    apply_theme(saved if saved in THEMES else 'light')
    w = Main([p for p in sys.argv[1:] if os.path.isfile(p)])
    w.show()
    sys.exit(app.exec())


if __name__ == '__main__':
    main()