#!/usr/bin/env python3
# kids_money_qt.py
# PyQt6 GUI to manage kids' cash + silver + stocks with a local SQLite DB.
#
# Run:
#   python3 kids_money_qt.py
#
# Install deps:
#   pip install PyQt6
#
# DB:
#   kids_money.sqlite3 in current folder (configurable inside code)

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Tuple, Dict

from PyQt6.QtCore import Qt, QAbstractTableModel, QModelIndex
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QComboBox, QLineEdit, QDoubleSpinBox, QSpinBox,
    QTableView, QGroupBox, QMessageBox, QFormLayout, QSplitter,
    QTabWidget, QHeaderView, QCheckBox
)

DB_PATH = Path("kids_money.sqlite3")


# ---------------------------- DB / Ledger ----------------------------

def now_iso() -> str:
    return datetime.now().replace(microsecond=0).isoformat(sep=" ")


def connect(db_path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON;")
    return con


def init_db(con: sqlite3.Connection) -> None:
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS children (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE
        );

        -- Transaction types:
        -- CASH_DEPOSIT, CASH_WITHDRAW
        -- SILVER_TRADE, STOCK_TRADE
        CREATE TABLE IF NOT EXISTS tx (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            child_id INTEGER NOT NULL,
            type TEXT NOT NULL,
            asset TEXT,              -- "SILVER" or "STOCK:<SYMBOL>"
            qty REAL,                -- for trades: + buy, - sell
            price_eur REAL,          -- unit price for trades
            cash_delta_eur REAL NOT NULL, -- + increases cash, - decreases cash
            note TEXT,
            FOREIGN KEY(child_id) REFERENCES children(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_tx_child_ts ON tx(child_id, ts);
        """
    )
    con.commit()


def list_children(con: sqlite3.Connection) -> List[str]:
    rows = con.execute("SELECT name FROM children ORDER BY name").fetchall()
    return [r["name"] for r in rows]


def add_child(con: sqlite3.Connection, name: str) -> None:
    name = name.strip()
    if not name:
        raise ValueError("Name ist leer.")
    try:
        con.execute("INSERT INTO children(name) VALUES (?)", (name,))
        con.commit()
    except sqlite3.IntegrityError:
        raise ValueError(f"Kind '{name}' existiert bereits.")


def get_child_id(con: sqlite3.Connection, name: str) -> int:
    row = con.execute("SELECT id FROM children WHERE name=?", (name,)).fetchone()
    if not row:
        raise ValueError(f"Kind '{name}' nicht gefunden.")
    return int(row["id"])


def remove_child(con: sqlite3.Connection, name: str) -> None:
    """Deletes a child account (and all transactions via ON DELETE CASCADE)."""
    cid = get_child_id(con, name)
    con.execute("DELETE FROM children WHERE id=?", (cid,))
    con.commit()


def get_tx_by_id(con: sqlite3.Connection, tx_id: int) -> sqlite3.Row:
    row = con.execute("SELECT * FROM tx WHERE id=?", (tx_id,)).fetchone()
    if not row:
        raise ValueError(f"Transaktion #{tx_id} nicht gefunden.")
    return row


def undo_transaction(con: sqlite3.Connection, tx_id: int) -> None:
    """Undo a transaction by inserting an opposite 'UNDO' booking.

    This keeps the ledger immutable (no DELETE from tx). It validates that the
    reversal is possible with current balances/holdings.
    """
    r = get_tx_by_id(con, tx_id)
    child_id = int(r["child_id"])
    asset = r["asset"]
    qty = r["qty"]
    price = r["price_eur"]
    cash_delta = float(r["cash_delta_eur"])
    type_ = r["type"]
    ts = r["ts"]
    orig_note = (r["note"] or "").strip()

    rev_cash = -cash_delta
    rev_qty = -float(qty) if qty is not None else None

    # Validate reversal against current state
    if rev_cash < -1e-12:
        validate_withdraw(con, child_id, -rev_cash)
    if rev_qty is not None and rev_qty < -1e-12 and asset:
        validate_sell_asset(con, child_id, asset, -rev_qty)

    undo_note = f"UNDO tx#{tx_id} ({type_} @ {ts})"
    if orig_note:
        undo_note += f" | {orig_note}"

    insert_tx(
        con,
        child_id,
        "UNDO",
        cash_delta=rev_cash,
        asset=asset,
        qty=rev_qty,
        price=price,
        note=undo_note,
    )

def cash_balance(con: sqlite3.Connection, child_id: int) -> float:
    row = con.execute(
        "SELECT COALESCE(SUM(cash_delta_eur), 0) AS bal FROM tx WHERE child_id=?",
        (child_id,),
    ).fetchone()
    return float(row["bal"])


def asset_qty(con: sqlite3.Connection, child_id: int, asset: str) -> float:
    row = con.execute(
        "SELECT COALESCE(SUM(qty),0) AS q FROM tx WHERE child_id=? AND asset=?",
        (child_id, asset),
    ).fetchone()
    return float(row["q"])


def insert_tx(
    con: sqlite3.Connection,
    child_id: int,
    type_: str,
    cash_delta: float,
    asset: Optional[str] = None,
    qty: Optional[float] = None,
    price: Optional[float] = None,
    note: str = "",
    ts: Optional[str] = None,
) -> None:
    con.execute(
        """
        INSERT INTO tx(ts, child_id, type, asset, qty, price_eur, cash_delta_eur, note)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (ts or now_iso(), child_id, type_, asset, qty, price, cash_delta, note),
    )
    con.commit()


def avg_cost(con: sqlite3.Connection, child_id: int, asset: str) -> Tuple[float, float]:
    rows = con.execute(
        """
        SELECT ts, qty, price_eur, id
        FROM tx
        WHERE child_id=? AND asset=? AND qty IS NOT NULL
        ORDER BY ts ASC, id ASC
        """,
        (child_id, asset),
    ).fetchall()

    qty = 0.0
    cost_basis = 0.0
    for r in rows:
        q = float(r["qty"])
        p = float(r["price_eur"] or 0.0)
        if q > 0:
            qty += q
            cost_basis += q * p
        elif q < 0:
            sell_qty = -q
            if qty <= 0:
                qty -= sell_qty
                cost_basis -= sell_qty * p
            else:
                current_avg = cost_basis / qty if qty else 0.0
                qty -= sell_qty
                cost_basis -= sell_qty * current_avg

    avg = cost_basis / qty if qty > 1e-12 else 0.0
    return qty, avg


def validate_withdraw(con: sqlite3.Connection, child_id: int, amount: float) -> None:
    bal = cash_balance(con, child_id)
    if amount > bal + 1e-9:
        raise ValueError(f"Nicht genug Cash. Verfügbar: {bal:.2f} EUR")


def validate_sell_asset(con: sqlite3.Connection, child_id: int, asset: str, qty: float) -> None:
    have = asset_qty(con, child_id, asset)
    if qty > have + 1e-9:
        if asset == "SILVER":
            raise ValueError(f"Nicht genug Silber. Verfügbar: {have:g} Stück")
        if asset.startswith("STOCK:"):
            sym = asset.split(":", 1)[1]
            raise ValueError(f"Nicht genug {sym}. Verfügbar: {have:g} Stück")
        raise ValueError(f"Nicht genug Bestand. Verfügbar: {have:g}")


def deposit(con: sqlite3.Connection, child_id: int, amount: float, note: str) -> None:
    if amount <= 0:
        raise ValueError("Betrag muss > 0 sein.")
    insert_tx(con, child_id, "CASH_DEPOSIT", cash_delta=amount, note=note)


def withdraw(con: sqlite3.Connection, child_id: int, amount: float, note: str) -> None:
    if amount <= 0:
        raise ValueError("Betrag muss > 0 sein.")
    validate_withdraw(con, child_id, amount)
    insert_tx(con, child_id, "CASH_WITHDRAW", cash_delta=-amount, note=note)


def buy_silver(con: sqlite3.Connection, child_id: int, qty: float, price: float, note: str) -> None:
    if qty <= 0:
        raise ValueError("Stückzahl muss > 0 sein.")
    if price < 0:
        raise ValueError("Preis muss >= 0 sein.")
    cash_needed = qty * price
    validate_withdraw(con, child_id, cash_needed)
    insert_tx(con, child_id, "SILVER_TRADE", asset="SILVER", qty=qty, price=price, cash_delta=-cash_needed, note=note)


def sell_silver(con: sqlite3.Connection, child_id: int, qty: float, price: float, note: str) -> None:
    if qty <= 0:
        raise ValueError("Stückzahl muss > 0 sein.")
    if price < 0:
        raise ValueError("Preis muss >= 0 sein.")
    validate_sell_asset(con, child_id, "SILVER", qty)
    cash_gain = qty * price
    insert_tx(con, child_id, "SILVER_TRADE", asset="SILVER", qty=-qty, price=price, cash_delta=+cash_gain, note=note)


def buy_stock(con: sqlite3.Connection, child_id: int, symbol: str, qty: float, price: float, note: str) -> None:
    symbol = symbol.strip().upper()
    if not symbol:
        raise ValueError("Symbol fehlt (z.B. AAPL).")
    if qty <= 0:
        raise ValueError("Stückzahl muss > 0 sein.")
    if price < 0:
        raise ValueError("Preis muss >= 0 sein.")
    asset = f"STOCK:{symbol}"
    cash_needed = qty * price
    validate_withdraw(con, child_id, cash_needed)
    insert_tx(con, child_id, "STOCK_TRADE", asset=asset, qty=qty, price=price, cash_delta=-cash_needed, note=note)


def sell_stock(con: sqlite3.Connection, child_id: int, symbol: str, qty: float, price: float, note: str) -> None:
    symbol = symbol.strip().upper()
    if not symbol:
        raise ValueError("Symbol fehlt (z.B. AAPL).")
    if qty <= 0:
        raise ValueError("Stückzahl muss > 0 sein.")
    if price < 0:
        raise ValueError("Preis muss >= 0 sein.")
    asset = f"STOCK:{symbol}"
    validate_sell_asset(con, child_id, asset, qty)
    cash_gain = qty * price
    insert_tx(con, child_id, "STOCK_TRADE", asset=asset, qty=-qty, price=price, cash_delta=+cash_gain, note=note)


def fetch_overview_rows(con: sqlite3.Connection) -> List[Dict]:
    kids = list_children(con)
    out = []
    for name in kids:
        cid = get_child_id(con, name)
        cash = cash_balance(con, cid)

        # silver qty
        silver = asset_qty(con, cid, "SILVER")

        # all stock assets
        stock_assets = con.execute(
            """
            SELECT asset, COALESCE(SUM(qty),0) AS q
            FROM tx
            WHERE child_id=? AND asset LIKE 'STOCK:%'
            GROUP BY asset
            HAVING ABS(COALESCE(SUM(qty),0)) > 1e-12
            ORDER BY asset
            """,
            (cid,),
        ).fetchall()

        stocks = []
        for r in stock_assets:
            asset = r["asset"]
            q = float(r["q"])
            sym = asset.split(":", 1)[1]
            stocks.append((sym, q))

        out.append({
            "name": name,
            "cash": cash,
            "silver_qty": silver,
            "stocks": stocks,
        })
    return out


def fetch_holdings(con: sqlite3.Connection, child_id: int) -> List[Dict]:
    assets = con.execute(
        """
        SELECT asset
        FROM tx
        WHERE child_id=? AND asset IS NOT NULL
        GROUP BY asset
        ORDER BY asset
        """,
        (child_id,),
    ).fetchall()

    rows = []
    for r in assets:
        asset = r["asset"]
        q = asset_qty(con, child_id, asset)
        if abs(q) < 1e-12:
            continue
        qty, avg = avg_cost(con, child_id, asset)
        kind = "Silber" if asset == "SILVER" else "Aktie"
        sym = "" if asset == "SILVER" else asset.split(":", 1)[1]
        rows.append({
            "kind": kind,
            "symbol": sym,
            "qty": qty,
            "avg_cost": avg,
        })
    return rows


def fetch_statement(con: sqlite3.Connection, child_id: int, limit: int) -> List[Dict]:
    rows = con.execute(
        """
        SELECT id, ts, type, asset, qty, price_eur, cash_delta_eur, note
        FROM tx
        WHERE child_id=?
        ORDER BY ts DESC, id DESC
        LIMIT ?
        """,
        (child_id, limit),
    ).fetchall()

    out = []
    for r in rows:
        asset = r["asset"] or ""
        display_asset = ""
        if asset == "SILVER":
            display_asset = "SILVER"
        elif asset.startswith("STOCK:"):
            display_asset = "STOCK " + asset.split(":", 1)[1]
        else:
            display_asset = asset

        out.append({
            "id": r["id"],
            "ts": r["ts"],
            "type": r["type"],
            "asset": display_asset,
            "qty": r["qty"],
            "price": r["price_eur"],
            "cash_delta": r["cash_delta_eur"],
            "note": r["note"] or "",
        })
    return out


def price_to_eur(price_foreign: float, currency: str, fx_rate: float) -> float:
    """
    Converts a foreign price into EUR.
    fx_rate must be: 1 unit foreign currency = x EUR
    For EUR, fx_rate should be 1.
    """
    if currency == "EUR":
        return price_foreign
    return price_foreign * fx_rate


# ---------------------------- Table Models ----------------------------

class SimpleTableModel(QAbstractTableModel):
    def __init__(self, headers: List[str], rows: List[List[str]]):
        super().__init__()
        self._headers = headers
        self._rows = rows

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return len(self._rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return len(self._headers)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.EditRole):
            return self._rows[index.row()][index.column()]
        if role == Qt.ItemDataRole.TextAlignmentRole:
            return int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft)
        return None

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole):
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if orientation == Qt.Orientation.Horizontal:
            return self._headers[section]
        return str(section + 1)

    def set_rows(self, rows: List[List[str]]):
        self.beginResetModel()
        self._rows = rows
        self.endResetModel()


# ---------------------------- UI helpers ----------------------------

def err_box(parent: QWidget, title: str, msg: str):
    QMessageBox.critical(parent, title, msg)


def info_box(parent: QWidget, title: str, msg: str):
    QMessageBox.information(parent, title, msg)


def fmt_num(x: Optional[float], decimals: int = 2) -> str:
    if x is None:
        return ""
    return f"{float(x):.{decimals}f}"


def fmt_qty(x: Optional[float]) -> str:
    if x is None:
        return ""
    v = float(x)
    # show fewer decimals if it's basically integer
    if abs(v - round(v)) < 1e-9:
        return f"{int(round(v))}"
    return f"{v:g}"


# ---------------------------- Main Window ----------------------------

class MainWindow(QMainWindow):
    def __init__(self, con: sqlite3.Connection):
        super().__init__()
        self.con = con
        self.setWindowTitle("Kinder-Geldverwaltung (Cash / Silber / Aktien)")
        self.resize(1100, 650)

        root = QWidget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)

        # Top bar: child selection + add child
        top = QHBoxLayout()
        layout.addLayout(top)

        top.addWidget(QLabel("Kind:"))
        self.child_combo = QComboBox()
        self.child_combo.currentIndexChanged.connect(self.refresh_all)
        top.addWidget(self.child_combo, 1)

        self.add_child_name = QLineEdit()
        self.add_child_name.setPlaceholderText("Neues Kind (Name)")
        top.addWidget(self.add_child_name)

        add_btn = QPushButton("Kind hinzufügen")
        add_btn.clicked.connect(self.on_add_child)
        top.addWidget(add_btn)

        del_btn = QPushButton("Kind löschen")
        del_btn.clicked.connect(self.on_delete_child)
        top.addWidget(del_btn)

        self.cash_label = QLabel("Cash: -")
        self.cash_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        top.addWidget(self.cash_label)

        # Tabs
        tabs = QTabWidget()
        layout.addWidget(tabs, 1)

        # Tab: Bookings
        bookings = QWidget()
        tabs.addTab(bookings, "Buchen")
        b_layout = QHBoxLayout(bookings)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        b_layout.addWidget(splitter)

        # Left: forms
        forms_widget = QWidget()
        forms_layout = QVBoxLayout(forms_widget)
        forms_layout.setContentsMargins(8, 8, 8, 8)
        splitter.addWidget(forms_widget)

        # Right: overview + statement
        right_widget = QWidget()
        right_layout = QVBoxLayout(right_widget)
        right_layout.setContentsMargins(8, 8, 8, 8)
        splitter.addWidget(right_widget)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)

        # --- Cash group
        cash_group = QGroupBox("Cash")
        forms_layout.addWidget(cash_group)
        cash_form = QFormLayout(cash_group)

        self.cash_amount = QDoubleSpinBox()
        self.cash_amount.setRange(0, 1_000_000)
        self.cash_amount.setDecimals(2)
        self.cash_amount.setSingleStep(1.0)
        cash_form.addRow("Betrag (EUR):", self.cash_amount)

        self.cash_note = QLineEdit()
        self.cash_note.setPlaceholderText("Notiz (optional)")
        cash_form.addRow("Notiz:", self.cash_note)

        cash_btns = QHBoxLayout()
        btn_deposit = QPushButton("Einzahlen")
        btn_withdraw = QPushButton("Abheben")
        btn_deposit.clicked.connect(self.on_deposit)
        btn_withdraw.clicked.connect(self.on_withdraw)
        cash_btns.addWidget(btn_deposit)
        cash_btns.addWidget(btn_withdraw)
        cash_form.addRow(cash_btns)

        # --- Silver group
        silver_group = QGroupBox("Silbermünzen")
        forms_layout.addWidget(silver_group)
        silver_form = QFormLayout(silver_group)

        self.silver_qty = QDoubleSpinBox()
        self.silver_qty.setRange(0, 1_000_000)
        self.silver_qty.setDecimals(3)
        self.silver_qty.setSingleStep(1.0)
        silver_form.addRow("Stück:", self.silver_qty)

        self.silver_price = QDoubleSpinBox()
        self.silver_price.setRange(0, 1_000_000)
        self.silver_price.setDecimals(2)
        self.silver_price.setSingleStep(0.5)
        silver_form.addRow("Preis/Stück (EUR):", self.silver_price)

        self.silver_note = QLineEdit()
        self.silver_note.setPlaceholderText("z.B. Maple Leaf / von Oma / an Papa verkauft")
        silver_form.addRow("Notiz:", self.silver_note)

        silver_btns = QHBoxLayout()
        btn_buy_silver = QPushButton("Kaufen")
        btn_sell_silver = QPushButton("Verkaufen")
        btn_buy_silver.clicked.connect(self.on_buy_silver)
        btn_sell_silver.clicked.connect(self.on_sell_silver)
        silver_btns.addWidget(btn_buy_silver)
        silver_btns.addWidget(btn_sell_silver)
        silver_form.addRow(silver_btns)

        # --- Stock group
        stock_group = QGroupBox("Aktien")
        forms_layout.addWidget(stock_group)
        stock_form = QFormLayout(stock_group)

        self.stock_symbol = QLineEdit()
        self.stock_symbol.setPlaceholderText("z.B. AAPL, MSFT")
        stock_form.addRow("Symbol:", self.stock_symbol)

        self.stock_qty = QDoubleSpinBox()
        self.stock_qty.setRange(0, 1_000_000)
        self.stock_qty.setDecimals(6)
        self.stock_qty.setSingleStep(0.1)
        stock_form.addRow("Stück:", self.stock_qty)

        # Price in foreign currency
        self.stock_price_foreign = QDoubleSpinBox()
        self.stock_price_foreign.setRange(0, 1_000_000)
        self.stock_price_foreign.setDecimals(4)
        self.stock_price_foreign.setSingleStep(0.1)
        stock_form.addRow("Preis/Stück (Fremdwährung):", self.stock_price_foreign)

        # Currency selection
        self.stock_currency = QComboBox()
        self.stock_currency.addItems(["EUR", "USD", "GBP", "CHF"])
        stock_form.addRow("Währung:", self.stock_currency)

        # FX rate to EUR
        self.stock_fx_rate = QDoubleSpinBox()
        self.stock_fx_rate.setRange(0, 1000)
        self.stock_fx_rate.setDecimals(6)
        self.stock_fx_rate.setSingleStep(0.001)
        self.stock_fx_rate.setValue(1.0)
        stock_form.addRow("Wechselkurs → EUR:", self.stock_fx_rate)


        self.stock_note = QLineEdit()
        self.stock_note.setPlaceholderText("z.B. 'in Papas Depot' / Broker / Notiz")
        stock_form.addRow("Notiz:", self.stock_note)

        self.stock_use_note_flag = QCheckBox("Notiz automatisch: 'in Papas Depot'")
        self.stock_use_note_flag.setChecked(False)
        stock_form.addRow(self.stock_use_note_flag)

        stock_btns = QHBoxLayout()
        btn_buy_stock = QPushButton("Kaufen")
        btn_sell_stock = QPushButton("Verkaufen")
        btn_buy_stock.clicked.connect(self.on_buy_stock)
        btn_sell_stock.clicked.connect(self.on_sell_stock)
        stock_btns.addWidget(btn_buy_stock)
        stock_btns.addWidget(btn_sell_stock)
        stock_form.addRow(stock_btns)

        forms_layout.addStretch(1)

        # Right side: Overview table + holdings + statement
        # Overview (all kids)
        right_layout.addWidget(QLabel("Übersicht (alle Kinder):"))
        self.overview_table = QTableView()
        self.overview_model = SimpleTableModel(
            ["Kind", "Cash (EUR)", "Silber (Stück)", "Aktien (Symbol:Stück)"],
            []
        )
        self.overview_table.setModel(self.overview_model)
        self.overview_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        right_layout.addWidget(self.overview_table, 1)

        # Holdings (selected kid)
        right_layout.addWidget(QLabel("Bestände (aktuelles Kind):"))
        self.holdings_table = QTableView()
        self.holdings_model = SimpleTableModel(
            ["Asset", "Symbol", "Stück", "Ø Einstand (EUR/Stück)"],
            []
        )
        self.holdings_table.setModel(self.holdings_model)
        self.holdings_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        right_layout.addWidget(self.holdings_table, 1)

        # Statement
        stmt_bar = QHBoxLayout()
        right_layout.addLayout(stmt_bar)
        stmt_bar.addWidget(QLabel("Kontoauszug (aktuelles Kind):"))
        stmt_bar.addStretch(1)
        stmt_bar.addWidget(QLabel("Limit:"))
        self.stmt_limit = QSpinBox()
        self.stmt_limit.setRange(10, 5000)
        self.stmt_limit.setValue(50)
        self.stmt_limit.valueChanged.connect(self.refresh_statement)
        stmt_bar.addWidget(self.stmt_limit)
        refresh_btn = QPushButton("Aktualisieren")
        refresh_btn.clicked.connect(self.refresh_all)
        stmt_bar.addWidget(refresh_btn)

        undo_btn = QPushButton("Buchung rückgängig")
        undo_btn.clicked.connect(self.on_undo_selected_tx)
        stmt_bar.addWidget(undo_btn)

        self.statement_table = QTableView()
        self.statement_model = SimpleTableModel(
            ["ID", "Zeit", "Typ", "Asset", "Stück", "Preis", "Cash Δ (EUR)", "Notiz"],
            []
        )
        self.statement_table.setModel(self.statement_model)
        self.statement_table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.statement_table.setSelectionMode(QTableView.SelectionMode.SingleSelection)
        self.statement_table.setColumnHidden(0, True)
        self.statement_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        right_layout.addWidget(self.statement_table, 2)

        # Initial load
        self.reload_children()
        self.refresh_all()

        # Tab: Setup/help (tiny)
        help_tab = QWidget()
        tabs.addTab(help_tab, "Info")
        hl = QVBoxLayout(help_tab)
        hl.addWidget(QLabel(
            "Tipps:\n"
            "- Du arbeitest transaktionsbasiert: Jede Buchung ist eine Zeile.\n"
            "- Verkauf/Abhebung wird validiert (kein negativer Bestand/Cash).\n"
            "- DB-Datei: kids_money.sqlite3 im Ordner.\n"
            "- Aktien-Asset wird als STOCK:<SYMBOL> gespeichert."
        ))
        hl.addStretch(1)

    # ---------- UI actions ----------

    def current_child(self) -> Optional[str]:
        if self.child_combo.count() == 0:
            return None
        return self.child_combo.currentText().strip() or None

    def current_child_id(self) -> Optional[int]:
        name = self.current_child()
        if not name:
            return None
        return get_child_id(self.con, name)

    def reload_children(self, keep: Optional[str] = None):
        kids = list_children(self.con)
        self.child_combo.blockSignals(True)
        self.child_combo.clear()
        self.child_combo.addItems(kids)
        if keep and keep in kids:
            self.child_combo.setCurrentText(keep)
        self.child_combo.blockSignals(False)

    def refresh_all(self):
        self.refresh_cash_label()
        self.refresh_overview()
        self.refresh_holdings()
        self.refresh_statement()

    def refresh_cash_label(self):
        cid = self.current_child_id()
        if cid is None:
            self.cash_label.setText("Cash: -")
            return
        self.cash_label.setText(f"Cash: {cash_balance(self.con, cid):.2f} EUR")

    def refresh_overview(self):
        rows = fetch_overview_rows(self.con)
        table_rows = []
        for r in rows:
            stocks = ", ".join([f"{sym}:{fmt_qty(q)}" for sym, q in r["stocks"]]) if r["stocks"] else ""
            table_rows.append([
                r["name"],
                fmt_num(r["cash"], 2),
                fmt_qty(r["silver_qty"]),
                stocks,
            ])
        self.overview_model.set_rows(table_rows)

    def refresh_holdings(self):
        cid = self.current_child_id()
        if cid is None:
            self.holdings_model.set_rows([])
            return
        rows = fetch_holdings(self.con, cid)
        table_rows = []
        for r in rows:
            table_rows.append([
                r["kind"],
                r["symbol"],
                fmt_qty(r["qty"]),
                fmt_num(r["avg_cost"], 2),
            ])
        self.holdings_model.set_rows(table_rows)

    def refresh_statement(self):
        cid = self.current_child_id()
        if cid is None:
            self.statement_model.set_rows([])
            return
        limit = int(self.stmt_limit.value())
        rows = fetch_statement(self.con, cid, limit)
        table_rows = []
        for r in rows:
            table_rows.append([
                str(r["id"]),
                r["ts"],
                r["type"],
                r["asset"],
                fmt_qty(r["qty"]),
                fmt_num(r["price"], 4) if r["price"] is not None else "",
                fmt_num(r["cash_delta"], 2),
                r["note"],
            ])
        self.statement_model.set_rows(table_rows)

    def on_add_child(self):
        name = self.add_child_name.text().strip()
        try:
            add_child(self.con, name)
        except Exception as e:
            err_box(self, "Fehler", str(e))
            return
        self.add_child_name.clear()
        self.reload_children(keep=name)
        self.refresh_all()
        info_box(self, "OK", f"Kind '{name}' hinzugefügt.")

    def on_deposit(self):
        cid = self.current_child_id()
        if cid is None:
            err_box(self, "Fehler", "Bitte zuerst ein Kind auswählen.")
            return
        amount = float(self.cash_amount.value())
        note = self.cash_note.text().strip()
        try:
            deposit(self.con, cid, amount, note)
        except Exception as e:
            err_box(self, "Fehler", str(e))
            return
        self.refresh_all()

    def on_withdraw(self):
        cid = self.current_child_id()
        if cid is None:
            err_box(self, "Fehler", "Bitte zuerst ein Kind auswählen.")
            return
        amount = float(self.cash_amount.value())
        note = self.cash_note.text().strip()
        try:
            withdraw(self.con, cid, amount, note)
        except Exception as e:
            err_box(self, "Fehler", str(e))
            return
        self.refresh_all()

    def on_buy_silver(self):
        cid = self.current_child_id()
        if cid is None:
            err_box(self, "Fehler", "Bitte zuerst ein Kind auswählen.")
            return
        qty = float(self.silver_qty.value())
        price = float(self.silver_price.value())
        note = self.silver_note.text().strip()
        try:
            buy_silver(self.con, cid, qty, price, note)
        except Exception as e:
            err_box(self, "Fehler", str(e))
            return
        self.refresh_all()

    def on_sell_silver(self):
        cid = self.current_child_id()
        if cid is None:
            err_box(self, "Fehler", "Bitte zuerst ein Kind auswählen.")
            return
        qty = float(self.silver_qty.value())
        price = float(self.silver_price.value())
        note = self.silver_note.text().strip()
        try:
            sell_silver(self.con, cid, qty, price, note)
        except Exception as e:
            err_box(self, "Fehler", str(e))
            return
        self.refresh_all()

    def on_buy_stock(self):
        cid = self.current_child_id()
        name = self.current_child()
        if cid is None or not name:
            err_box(self, "Fehler", "Bitte zuerst ein Kind auswählen.")
            return
        symbol = self.stock_symbol.text().strip().upper()
        qty = float(self.stock_qty.value())
        price_foreign = float(self.stock_price_foreign.value())
        currency = self.stock_currency.currentText()
        fx_rate = float(self.stock_fx_rate.value())

        price = price_to_eur(price_foreign, currency, 1/fx_rate)
        note = self.stock_note.text().strip()
        note_fx = f"{price_foreign:.4f} {currency} @ {fx_rate:.4f}"
        note = f"{note} | {note_fx}" if note else note_fx
        if self.stock_use_note_flag.isChecked() and not note:
            note = "in Papas Depot"
        try:
            buy_stock(self.con, cid, symbol, qty, price, note)
        except Exception as e:
            err_box(self, "Fehler", str(e))
            return
        self.refresh_all()

    def on_sell_stock(self):
        cid = self.current_child_id()
        name = self.current_child()
        if cid is None or not name:
            err_box(self, "Fehler", "Bitte zuerst ein Kind auswählen.")
            return
        symbol = self.stock_symbol.text().strip().upper()
        qty = float(self.stock_qty.value())
        price_foreign = float(self.stock_price_foreign.value())
        currency = self.stock_currency.currentText()
        fx_rate = float(self.stock_fx_rate.value())

        price = price_to_eur(price_foreign, currency, 1/fx_rate)
        note = self.stock_note.text().strip()
        note_fx = f"{price_foreign:.4f} {currency} @ {fx_rate:.4f}"
        note = f"{note} | {note_fx}" if note else note_fx
        if self.stock_use_note_flag.isChecked() and not note:
            note = "in Papas Depot"
        try:
            sell_stock(self.con, cid, symbol, qty, price, note)
        except Exception as e:
            err_box(self, "Fehler", str(e))
            return
        self.refresh_all()


    def on_delete_child(self):
        name = self.current_child()
        if not name:
            err_box(self, "Fehler", "Bitte zuerst ein Kind auswählen.")
            return

        reply = QMessageBox.question(
            self,
            "Kind löschen",
            f"Willst du das Konto von '{name}' wirklich löschen?\n"
            "Alle Buchungen werden ebenfalls gelöscht.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        try:
            remove_child(self.con, name)
        except Exception as e:
            err_box(self, "Fehler", str(e))
            return

        self.reload_children()
        self.refresh_all()
        info_box(self, "OK", f"Kind '{name}' gelöscht.")


    def on_undo_selected_tx(self):
        cid = self.current_child_id()
        if cid is None:
            err_box(self, "Fehler", "Bitte zuerst ein Kind auswählen.")
            return

        sel = self.statement_table.selectionModel().selectedRows()
        if not sel:
            err_box(self, "Fehler", "Bitte zuerst eine Buchung in der Tabelle auswählen.")
            return

        row = sel[0].row()
        try:
            tx_id = int(self.statement_model._rows[row][0])  # hidden ID column
        except Exception:
            err_box(self, "Fehler", "Konnte die Transaktions-ID nicht lesen.")
            return

        reply = QMessageBox.question(
            self,
            "Buchung rückgängig",
            f"Ausgewählte Buchung #{tx_id} wirklich rückgängig machen?\n"
            "Es wird eine Gegenbuchung (UNDO) erstellt.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        try:
            undo_transaction(self.con, tx_id)
        except Exception as e:
            err_box(self, "Fehler", str(e))
            return

        self.refresh_all()


def main():
    con = connect(DB_PATH)
    init_db(con)

    app = QApplication([])
    w = MainWindow(con)
    w.show()
    app.exec()


if __name__ == "__main__":
    main()
