"""
collect.py — Verdnatura Price Tracker v7
==========================================
Approche REST API pure (pas de Playwright pour la collecte).
Nouvelle version shop Quasar/Vue — API Loopback REST.

Flux :
  1. Login API  →  token
  2. POST /api/Orders/createMine  →  orderId
  3. GET  /api/itemCategories  →  8 catégories
  4. Pour chaque catégorie :
       GET /api/Orders/{id}/getItemTypeAvailable?itemCategoryId=N
       Pour chaque famille :
         GET /api/Orders/{id}/catalog?itemCategoryId=N&itemTypeId=M
  5. DELETE /api/Orders/{id}
  6. Stockage SQLite + export data.json
"""

import asyncio
import json
import os
import re
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

import requests as req

# ─────────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────────
SHOP_BASE     = "https://shop.verdnatura.es"
API_BASE      = f"{SHOP_BASE}/api"
COLLECT_TIERS = os.environ.get("COLLECT_TIERS", "0") == "1"

DB_PATH     = Path("verdnatura.db")
EXPORT_PATH = Path("data.json")


def load_credentials() -> tuple[str, str]:
    cfg = Path(__file__).parent / "config.json"
    if cfg.exists():
        d = json.loads(cfg.read_text(encoding="utf-8"))
        u, p = d.get("username", ""), d.get("password", "")
        if u and p: return u, p
    return os.environ.get("VERDNATURA_USER", ""), os.environ.get("VERDNATURA_PASS", "")


def get_period() -> str:
    h = datetime.now(timezone.utc).hour
    if h < 9:    return "matin"
    elif h < 14: return "midi"
    else:        return "soir"


def next_valid_date() -> date:
    """
    Date de livraison cible = J+2 minimum, ou le premier jour ouvré suivant
    parmi mardi/mercredi/jeudi/vendredi (weekday 1-4).
    On évite le lundi (souvent rupture après le week-end) et le J+1 (délai
    trop court pour certains articles en flux tendu).
    """
    d = date.today() + timedelta(days=2)
    while d.weekday() not in {1, 2, 3, 4}:  # mar, mer, jeu, ven
        d += timedelta(days=1)
    return d


# ─────────────────────────────────────────────
#  CLIENT REST
# ─────────────────────────────────────────────
class VerdnaturaAPI:
    def __init__(self, token: str):
        self.token   = token
        self.session = req.Session()
        self.session.headers.update({
            "Authorization": token,
            "Content-Type":  "application/json",
            "Accept":        "application/json",
            # Headers browser-like requis par certains endpoints
            "Origin":        SHOP_BASE,
            "Referer":       f"{SHOP_BASE}/",
        })

    def get(self, path: str, params: dict = None) -> list | dict | None:
        url = f"{API_BASE}/{path.lstrip('/')}"
        try:
            r = self.session.get(url, params=params, timeout=30)
            if r.status_code == 200:
                return r.json()
            print(f"    GET {path} → {r.status_code}: {r.text[:150]}")
        except Exception as e:
            print(f"    GET {path}: {e}")
        return None

    def post(self, path: str, body: dict = None) -> dict | None:
        url = f"{API_BASE}/{path.lstrip('/')}"
        try:
            r = self.session.post(url, json=body or {}, timeout=30)
            if r.status_code in (200, 204):
                return r.json() if r.content else {}
            print(f"    POST {path} → {r.status_code}: {r.text[:200]}")
        except Exception as e:
            print(f"    POST {path}: {e}")
        return None

    def delete(self, path: str) -> bool:
        url = f"{API_BASE}/{path.lstrip('/')}"
        try:
            r = self.session.delete(url, timeout=15)
            return r.status_code in (200, 204)
        except Exception:
            return False


# ─────────────────────────────────────────────
#  LOGIN
# ─────────────────────────────────────────────
def api_login(user: str, password: str) -> str | None:
    try:
        r = req.post(
            f"{API_BASE}/Accounts/login",
            json={"user": user, "password": password},
            headers={"Content-Type": "application/json"},
            timeout=15,
        )
        if r.status_code == 200:
            token = r.json().get("token") or r.json().get("id")
            if token:
                print(f"  ✅ Token obtenu")
                return token
    except Exception as e:
        print(f"  ❌ Login error: {e}")
    return None


# ─────────────────────────────────────────────
#  GESTION ORDRE
# ─────────────────────────────────────────────
def fetch_basket_defaults(api: VerdnaturaAPI) -> dict:
    """
    Récupère addressId et agencyModeId depuis plusieurs endpoints possibles.
    Retourne un dict avec 'addressId' et 'agencyModeId'.
    """
    candidates = [
        "Clients/myBasketDefaults",
        "Clients/myAddresses",
        "Addresses/myAddresses",
        "Clients/myDefault",
    ]
    for ep in candidates:
        raw = api.get(ep)
        if raw is None:
            continue
        item = raw[0] if isinstance(raw, list) and raw else raw if isinstance(raw, dict) else {}
        if not item:
            continue
        # Tenter toutes les variantes de noms de champ
        addr = (item.get("addressId") or item.get("addressFk") or
                item.get("defaultAddressId") or item.get("id"))
        agn  = (item.get("agencyModeId") or item.get("agencyModeFk") or
                item.get("agencyFk") or item.get("agencyId"))
        if addr and agn:
            print(f"  📋 Defaults via {ep} → addressId={addr} agencyModeId={agn}")
            return {"addressId": addr, "agencyModeId": agn}
        # Si on a l'adresse mais pas l'agence, garder pour plus tard
        if addr:
            print(f"  ⚠️  {ep} → addressId={addr} mais agencyModeId absent")
    return {}


def create_order(api: VerdnaturaAPI) -> int | None:
    """Crée une nouvelle commande avec les paramètres du compte."""

    defaults      = fetch_basket_defaults(api)
    address_id    = defaults.get("addressId", 78596)
    agency_mode   = defaults.get("agencyModeId", 639)
    delivery_date = next_valid_date()
    landed        = delivery_date.strftime("%Y-%m-%dT22:00:00.000Z")

    print(f"  📅 {delivery_date.strftime('%d/%m/%Y')}  |  addressId={address_id}  agencyModeId={agency_mode}")

    # L'API attend agencyModeId (pas agencyModeFk) — corrigé v7.1
    body = {
        "addressId":    address_id,
        "agencyModeId": agency_mode,
        "landed":       landed,
    }
    order = api.post("Orders/createMine", body=body)
    if order and "id" in order:
        order_id = order["id"]
        print(f"  ✅ Commande créée : #{order_id}")
        time.sleep(1)
        return order_id

    # Fallback : essayer sans agencyModeId (certaines versions de l'API l'ignorent)
    print("  🔄 Retry sans agencyModeId…")
    body2 = {"addressId": address_id, "landed": landed}
    order2 = api.post("Orders/createMine", body=body2)
    if order2 and "id" in order2:
        order_id = order2["id"]
        print(f"  ✅ Commande créée (fallback) : #{order_id}")
        time.sleep(1)
        return order_id

    print("  ❌ Création commande échouée")
    return None


def delete_order(api: VerdnaturaAPI, order_id: int) -> bool:
    """Supprime la commande créée pour la collecte."""
    ok = api.delete(f"Orders/{order_id}")
    print(f"  {'✅' if ok else '⚠️'} Commande #{order_id} {'supprimée' if ok else 'non supprimée — supprimer manuellement'}")
    return ok


# ─────────────────────────────────────────────
#  COLLECTE CATALOGUE VIA API
# ─────────────────────────────────────────────
# Catégories à collecter (probe confirmed, v7.5)
# cat 6/8/9/14/16/17 = services, emballages, mascotas, matériel → ignorées
COLLECT_CAT_IDS = {1, 2, 3, 4, 5, 7, 10, 13}

CATEGORY_LABELS = {
    1:  "🌸 Fleurs coupées",
    2:  "🪴 Plantes",
    3:  "🎀 Compléments floraux",
    4:  "🌿 Artificiel",
    5:  "🍃 Feuillages frais",
    7:  "💐 Confection naturelle",
    10: "🎋 Confection artificielle",
    13: "🌾 Sec & Préservé",
}


def get_all_item_types(api: VerdnaturaAPI) -> dict[int, list[dict]]:
    """
    Récupère les 213 types via itemTypes (accès global confirmé par probe).
    Retourne un dict {cat_id: [types]} filtré sur COLLECT_CAT_IDS.
    """
    all_types = api.get("itemTypes")
    if not all_types:
        return {}
    by_cat: dict[int, list[dict]] = {}
    for t in all_types:
        cat_id = t.get("categoryFk") or t.get("itemCategoryFk")
        if cat_id in COLLECT_CAT_IDS:
            by_cat.setdefault(cat_id, []).append(t)
    return by_cat


def get_categories(api: VerdnaturaAPI) -> list[dict]:
    """Récupère les catégories avec leurs familles — fallback si itemTypes indisponible."""
    cats = api.get("itemCategories", params={
        "filter": json.dumps({
            "include": {"relation": "itemTypes", "scope": {"fields": ["id", "code", "name"]}}
        })
    })
    return [c for c in (cats or []) if c.get("id") in COLLECT_CAT_IDS]


def get_item_types_for_cat(api: VerdnaturaAPI, cat_id: int) -> list[dict]:
    """Fallback par catégorie si get_all_item_types échoue."""
    types = api.get("itemTypes", params={
        "filter": json.dumps({"where": {"categoryFk": cat_id}})
    })
    return types or []


def get_products_for_type(api: VerdnaturaAPI, order_id: int, cat_id: int, type_id: int) -> list[dict]:
    """
    Récupère les produits d'une catégorie + famille.
    URL observée dans Network (v7.2) :
      Items/catalog?orderFk=9031801&orderBy=i.relevancy+DESC,+longName&categoryFk=1&typeFk=2
    orderBy est OBLIGATOIRE (l'API renvoie 400 sans lui).
    """
    result = api.get("Items/catalog", params={
        "orderFk":    order_id,
        "categoryFk": cat_id,
        "typeFk":     type_id,
        "orderBy":    "i.relevancy DESC, longName",
    })
    if result and isinstance(result, list):
        return result
    return []


# Variable globale pour le debug des champs (affiché une seule fois)
_FIELDS_DUMPED = False

def normalize_product(raw: dict, category: str, subcategory: str) -> dict | None:
    """
    Normalise un produit brut vers le format DB.
    Champs réels observés dans l'API Verdnatura (v7.2) :
      longName, price, packing, available, itemFk, producer, etc.
    """
    # Champs réels confirmés par dump API (v7.3) :
    # id=191975, item="· Alstroemeria Blanca Select", subName="Funza Maritimo",
    # producer, origin, available, price, grouping, image, updated, relevancy,
    # size, ink, minQuantity, tag5/value5..tag8/value8

    global _FIELDS_DUMPED
    if not _FIELDS_DUMPED:
        import json as _json
        print(f"\n  🔍 [DEBUG] 1er produit : {_json.dumps(raw, ensure_ascii=False)[:400]}")
        _FIELDS_DUMPED = True

    # Nom — champ "item" sur Verdnatura (pas name/longName)
    name = (raw.get("item") or raw.get("longName") or raw.get("name") or
            raw.get("itemName") or "").strip().lstrip("· ").strip()
    if not name:
        return None

    # Référence unique = id produit (stable entre snapshots)
    ref = str(raw.get("id") or raw.get("itemFk") or name)

    # Prix UNITAIRE (déjà à la tige/pièce — Verdnatura affiche le prix par unité,
    # ex: "0.32€ x10" = 0.32€/tige avec minimum d'achat 10 tiges, PAS 0.32€ pour le lot de 10)
    try:
        price = float(raw.get("price") or raw.get("rate1") or 0)
    except (TypeError, ValueError):
        price = 0.0

    # Quantité MINIMUM d'achat (pas un multiplicateur de prix !)
    # grouping=10 signifie "vendu par lots de 10 minimum", le prix ci-dessus
    # est déjà celui d'UNE tige/pièce.
    try:
        grouping = int(raw.get("grouping") or raw.get("packing") or raw.get("minQuantity") or 1)
    except (TypeError, ValueError):
        grouping = 1

    # Stock disponible
    try:
        available = int(raw.get("available") or raw.get("stock") or 0)
    except (TypeError, ValueError):
        available = 0

    # Producteur / origine — subName est le champ principal
    sub_name = str(raw.get("subName") or raw.get("producer") or
                   raw.get("origin") or "").strip()

    # Taille (cm) et couleur extraites des tags
    size  = raw.get("size") or ""
    color = raw.get("ink") or raw.get("value5") or ""

    # Caractéristiques dynamiques — Verdnatura expose tag1/value1 .. tag8/value8
    # (Couleur, Altura/Hauteur, Origen, Diámetro, Ancho, etc. selon le type de produit).
    # On les capture génériquement pour ne rater aucune info produit.
    attrs = {}
    for i in range(1, 10):
        tag = raw.get(f"tag{i}")
        val = raw.get(f"value{i}")
        if tag and val not in (None, ""):
            attrs[str(tag).strip()] = str(val).strip()

    return {
        "ref":         ref,
        "name":        name,
        "sub_name":    sub_name,
        "category":    category,
        "subcategory": subcategory,
        "price":       price,
        "grouping":    grouping,
        "available":   available,
        "size":        str(size) if size else "",
        "color":       color,
        "attrs":       attrs,
        "price_tiers": {},
    }


# ─────────────────────────────────────────────
#  SQLITE
# ─────────────────────────────────────────────
def init_db(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS products (
            ref         TEXT PRIMARY KEY,
            name        TEXT NOT NULL,
            sub_name    TEXT DEFAULT '',
            category    TEXT DEFAULT '',
            subcategory TEXT DEFAULT '',
            grouping    INTEGER DEFAULT 1,
            price_tiers TEXT DEFAULT '{}',
            first_seen  TEXT NOT NULL,
            last_seen   TEXT NOT NULL,
            is_active   INTEGER DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS snapshots (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            ref       TEXT NOT NULL,
            price     REAL NOT NULL,
            available INTEGER DEFAULT 0,
            timestamp TEXT NOT NULL,
            period    TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_snap_ref    ON snapshots(ref);
        CREATE INDEX IF NOT EXISTS idx_snap_ts     ON snapshots(timestamp);
        CREATE INDEX IF NOT EXISTS idx_snap_ref_ts ON snapshots(ref, timestamp);
    """)
    conn.commit()
    for col in [
        "price_tiers TEXT DEFAULT '{}'",
        "sub_name TEXT DEFAULT ''",
        "size TEXT DEFAULT ''",
        "color TEXT DEFAULT ''",
        "attrs TEXT DEFAULT '{}'",
    ]:
        try:
            conn.execute(f"ALTER TABLE products ADD COLUMN {col}")
            conn.commit()
        except Exception:
            pass


def save_snapshot(conn, products, now, period):
    cur = conn.cursor()
    cur.execute("SELECT ref FROM products WHERE is_active=1")
    active = {r[0] for r in cur.fetchall()}
    cur.execute("SELECT ref FROM products WHERE is_active=0")
    inactive = {r[0] for r in cur.fetchall()}
    current = {p["ref"] for p in products}

    gone = active - current
    if gone:
        conn.execute(
            f"UPDATE products SET is_active=0,last_seen=? WHERE ref IN ({','.join('?'*len(gone))})",
            [now, *gone])
    back = inactive & current
    if back:
        conn.execute(
            f"UPDATE products SET is_active=1,last_seen=? WHERE ref IN ({','.join('?'*len(back))})",
            [now, *back])

    new_count = 0
    for p in products:
        ref = p["ref"]
        if p.get("price_tiers"):
            conn.execute("UPDATE products SET price_tiers=? WHERE ref=?",
                         [json.dumps(p["price_tiers"], ensure_ascii=False), ref])
        if ref in active or ref in back:
            conn.execute(
                "UPDATE products SET last_seen=?,name=?,sub_name=?,category=?,subcategory=?,grouping=?,size=?,color=?,attrs=? WHERE ref=?",
                [now, p["name"], p.get("sub_name",""), p["category"], p["subcategory"], p["grouping"],
                 p.get("size",""), p.get("color",""), json.dumps(p.get("attrs",{}), ensure_ascii=False), ref])
        else:
            conn.execute(
                "INSERT OR IGNORE INTO products "
                "(ref,name,sub_name,category,subcategory,grouping,price_tiers,size,color,attrs,first_seen,last_seen,is_active) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1)",
                [ref, p["name"], p.get("sub_name",""), p["category"], p["subcategory"],
                 p["grouping"], json.dumps(p.get("price_tiers",{})), p.get("size",""), p.get("color",""),
                 json.dumps(p.get("attrs",{}), ensure_ascii=False), now, now])
            new_count += 1
        if p.get("price", 0) > 0:
            conn.execute(
                "INSERT INTO snapshots (ref,price,available,timestamp,period) VALUES (?,?,?,?,?)",
                [ref, p["price"], p.get("available", 0), now, period])
    conn.commit()
    return {"new": new_count, "disappeared": len(gone), "reappeared": len(back)}


# ─────────────────────────────────────────────
#  EXPORT JSON
# ─────────────────────────────────────────────
def export_json(conn):
    cur = conn.cursor()
    now = datetime.now(timezone.utc).isoformat()
    def q1(sql): cur.execute(sql); return cur.fetchone()[0]

    total  = q1("SELECT COUNT(*) FROM products")
    active = q1("SELECT COUNT(*) FROM products WHERE is_active=1")
    snaps  = q1("SELECT COUNT(*) FROM snapshots")
    new7   = q1("SELECT COUNT(*) FROM products WHERE first_seen>=datetime('now','-7 days')")
    lost7  = q1("SELECT COUNT(*) FROM products WHERE is_active=0 AND last_seen>=datetime('now','-7 days')")

    cur.execute("""
        SELECT p.ref,p.name,p.sub_name,p.category,p.subcategory,p.grouping,p.price_tiers,
               p.size,p.color,p.attrs,
               p.first_seen,p.last_seen,p.is_active,
               COUNT(s.id),MIN(s.price),MAX(s.price),ROUND(AVG(s.price),4),
               (SELECT price FROM snapshots WHERE ref=p.ref ORDER BY timestamp DESC LIMIT 1),
               (SELECT available FROM snapshots WHERE ref=p.ref ORDER BY timestamp DESC LIMIT 1)
        FROM products p LEFT JOIN snapshots s ON s.ref=p.ref
        GROUP BY p.ref ORDER BY p.category,p.subcategory,p.name
    """)
    cols = ["ref","name","sub_name","category","subcategory","grouping","price_tiers",
            "size","color","attrs",
            "first_seen","last_seen","is_active","snap_count",
            "price_min","price_max","price_avg","price_last","available_last"]
    prods = [dict(zip(cols, r)) for r in cur.fetchall()]

    for p in prods:
        p["is_active"] = bool(p["is_active"])
        try: p["price_tiers"] = json.loads(p["price_tiers"] or "{}")
        except: p["price_tiers"] = {}
        try: p["attrs"] = json.loads(p["attrs"] or "{}")
        except: p["attrs"] = {}
        cur.execute("""
            SELECT timestamp,price FROM snapshots
            WHERE ref=? AND timestamp>=datetime('now','-30 days') ORDER BY timestamp
        """, [p["ref"]])
        p["trend"] = _trend(cur.fetchall())

    cur.execute("""
        SELECT ref,CAST(strftime('%s',timestamp) AS INTEGER),price,period
        FROM snapshots WHERE timestamp>=datetime('now','-60 days')
        ORDER BY ref,timestamp
    """)
    history = {}
    for ref, ts, price, period in cur.fetchall():
        history.setdefault(ref, []).append([ts, round(price, 4), period])

    cur.execute("SELECT ref,name,category,first_seen FROM products WHERE first_seen>=datetime('now','-14 days') ORDER BY first_seen DESC LIMIT 50")
    new_feed  = [{"ref":r[0],"name":r[1],"category":r[2],"date":r[3]} for r in cur.fetchall()]
    cur.execute("SELECT ref,name,category,last_seen FROM products WHERE is_active=0 AND last_seen>=datetime('now','-14 days') ORDER BY last_seen DESC LIMIT 50")
    lost_feed = [{"ref":r[0],"name":r[1],"category":r[2],"date":r[3]} for r in cur.fetchall()]

    payload = {
        "generated": now,
        "stats": {"total":total,"active":active,"inactive":total-active,
                  "new_7d":new7,"lost_7d":lost7,"total_snapshots":snaps},
        "products": prods, "history": history,
        "recent_new": new_feed, "recent_lost": lost_feed,
    }
    EXPORT_PATH.write_text(json.dumps(payload, ensure_ascii=False, separators=(",",":")), encoding="utf-8")
    print(f"📤 data.json — {len(prods)} produits, {snaps} snapshots")


def _trend(history):
    if len(history) < 3: return None
    from datetime import datetime as dt
    xs = [dt.fromisoformat(r[0]).timestamp() for r in history]
    ys = [r[1] for r in history]
    n, mx, my = len(xs), sum(xs)/len(xs), sum(ys)/len(ys)
    d = sum((x-mx)**2 for x in xs)
    if d == 0: return None
    return round(sum((xs[i]-mx)*(ys[i]-my) for i in range(n))/d*86400, 6)


# ─────────────────────────────────────────────
#  NOMS DES CATÉGORIES
# ─────────────────────────────────────────────
CAT_NAMES = {
    "flower":  "🌸 Fleurs coupées",
    "plant":   "🪴 Plantes",
    "access":  "🎁 Accessoires",
    "greene":  "🍃 Verdure",
    "artifi":  "🌿 Artificiel",
    "preser":  "🌾 Séché / Préservé",
    "handma":  "💐 Bouquets / Compositions",
}

def cat_label(code: str, name: str) -> str:
    code_l = (code or "").lower()
    for k, v in CAT_NAMES.items():
        if k in code_l:
            return v
    return name


# ─────────────────────────────────────────────
#  SCRAPING PRINCIPAL
# ─────────────────────────────────────────────
def scrape_via_api(vn_user: str, vn_pass: str) -> list[dict]:
    all_products: list[dict] = []
    seen: set[str]           = set()

    # ── Login ──────────────────────────────────────────────────
    print("\n🔑 Login API…")
    token = api_login(vn_user, vn_pass)
    if not token:
        print("  ❌ Login échoué")
        return []

    api = VerdnaturaAPI(token)

    # ── Créer une commande temporaire ──────────────────────────
    print("\n📦 Création commande temporaire…")
    order_id = create_order(api)
    if not order_id:
        return []

    # ── Récupérer toutes les familles par catégorie ───────────
    print("\n🗂️  Récupération des familles…")
    types_by_cat = get_all_item_types(api)

    if not types_by_cat:
        # Fallback sur itemCategories
        print("  ⚠️  itemTypes indisponible, fallback sur itemCategories…")
        categories = get_categories(api)
        for c in categories:
            cid = c.get("id")
            types_by_cat[cid] = c.get("itemTypes") or get_item_types_for_cat(api, cid)

    total_types = sum(len(v) for v in types_by_cat.values())
    print(f"  {len(types_by_cat)} catégories, {total_types} familles au total")

    for cat_id in sorted(types_by_cat.keys()):
        item_types = types_by_cat[cat_id]
        cat_name   = CATEGORY_LABELS.get(cat_id, f"cat_{cat_id}")
        print(f"\n  📁 {cat_name} ({len(item_types)} familles)")

        for item_type in item_types:
            type_id   = item_type.get("id")
            type_name = item_type.get("name", f"type_{type_id}")
            print(f"    📂 {type_name}", end=" → ", flush=True)

            raw_prods = get_products_for_type(api, order_id, cat_id, type_id)

            if not raw_prods:
                print("0")
                continue

            new_count = 0
            for raw in raw_prods:
                p = normalize_product(raw, cat_name, type_name)
                if p and p["ref"] not in seen:
                    seen.add(p["ref"])
                    all_products.append(p)
                    new_count += 1

            print(f"{new_count} ({len(raw_prods)} reçus)")
            time.sleep(0.1)

    # ── Supprimer la commande temporaire ──────────────────────
    print(f"\n🗑️  Suppression commande #{order_id}…")
    delete_order(api, order_id)

    return all_products


# ─────────────────────────────────────────────
#  MODE DEBUG API
# ─────────────────────────────────────────────
def debug_api(vn_user: str, vn_pass: str):
    """
    Mode diagnostic : dump tous les endpoints utiles pour identifier
    la structure réelle de l'API Verdnatura.
    Usage : python collect.py --debug-api
    """
    print("\n🔬 MODE DEBUG API")
    print("="*54)

    print("\n🔑 Login…")
    token = api_login(vn_user, vn_pass)
    if not token:
        print("❌ Login échoué — vérifier config.json")
        sys.exit(1)

    api = VerdnaturaAPI(token)

    # ── 1. Endpoints de profil/defaults ───────────────────────
    print("\n📋 [1/4] Endpoints profil / adresses")
    probe_endpoints = [
        "Clients/myBasketDefaults",
        "Clients/myAddresses",
        "Clients/myDefault",
        "Addresses/myAddresses",
        "Clients/myData",
        "Accounts/myInfo",
    ]
    found_defaults = {}
    for ep in probe_endpoints:
        result = api.get(ep)
        if result is not None:
            item = result[0] if isinstance(result, list) and result else result
            print(f"\n  ✅ {ep}")
            print(f"     Clés : {list(item.keys()) if isinstance(item, dict) else type(result)}")
            print(f"     Valeur brute : {json.dumps(item if isinstance(item, dict) else result, ensure_ascii=False)[:300]}")
            if not found_defaults:
                found_defaults = item if isinstance(item, dict) else {}
        else:
            print(f"  ✗  {ep}")

    if not found_defaults:
        print("\n  ⚠️  Aucun endpoint de profil ne répond — arrêt du debug")
        sys.exit(1)

    # Extraire address et agency
    addr_id = (found_defaults.get("addressId") or found_defaults.get("addressFk") or
               found_defaults.get("defaultAddressId") or found_defaults.get("id"))
    agn_id  = (found_defaults.get("agencyModeId") or found_defaults.get("agencyModeFk") or
               found_defaults.get("agencyFk") or found_defaults.get("agencyId"))
    print(f"\n  → addressId détecté   : {addr_id}")
    print(f"  → agencyModeId détecté: {agn_id}")

    # ── 2. Création commande ───────────────────────────────────
    print("\n📦 [2/4] Tentatives createMine")
    delivery_date = next_valid_date()
    landed = delivery_date.strftime("%Y-%m-%dT22:00:00.000Z")

    order_id = None
    bodies = []
    if addr_id and agn_id:
        bodies.append({"label": "addressId+agencyModeId",
                        "body": {"addressId": addr_id, "agencyModeId": agn_id, "landed": landed}})
        bodies.append({"label": "addressId+agencyModeFk",
                        "body": {"addressId": addr_id, "agencyModeFk": agn_id, "landed": landed}})
    if addr_id:
        bodies.append({"label": "addressId seul",
                        "body": {"addressId": addr_id, "landed": landed}})
    bodies.append({"label": "body vide",
                   "body": {}})

    for attempt in bodies:
        print(f"\n  Essai [{attempt['label']}] → body={attempt['body']}")
        r = api.post("Orders/createMine", body=attempt["body"])
        if r and "id" in r:
            order_id = r["id"]
            print(f"  ✅ Commande créée : #{order_id}")
            break
        else:
            print(f"  ✗  Échec")

    if not order_id:
        print("\n  ❌ Impossible de créer une commande — debug limité")
        sys.exit(1)

    # ── 3. Endpoints catalogue ─────────────────────────────────
    print(f"\n🗂️  [3/4] Endpoints catalogue (orderId={order_id})")

    # Catégories
    cats = api.get("itemCategories", params={
        "filter": json.dumps({"where": {"display": True}, "order": "display ASC"})
    })
    if cats:
        print(f"\n  ✅ itemCategories → {len(cats)} catégories")
        for c in cats[:3]:
            print(f"     {c.get('id')} — {c.get('name')} [{c.get('code')}]")
    else:
        print("  ✗  itemCategories")
        cats = []

    # Familles pour 1ère catégorie
    if cats:
        cat0 = cats[0]
        cat_id = cat0["id"]
        print(f"\n  Familles pour catégorie id={cat_id}…")
        for ep_types in [
            f"Orders/{order_id}/getItemTypeAvailable",
            f"itemTypes",
        ]:
            params = {"itemCategoryId": cat_id} if "Orders" in ep_types else {"filter": json.dumps({"where": {"categoryFk": cat_id}})}
            types = api.get(ep_types, params=params)
            if types:
                t0 = types[0]
                print(f"  ✅ {ep_types} → {len(types)} familles")
                print(f"     Exemple clés : {list(t0.keys())}")
                type_id = t0.get("id")
                break
        else:
            type_id = None

        # Produits — vague 1 : endpoints classiques
        if type_id:
            print(f"\n  [3a] Endpoints produits classiques (cat={cat_id}, type={type_id})…")
            product_endpoints_v1 = [
                # LoopBack remote methods observés sur des SPA Vue/Quasar similaires
                (f"Orders/{order_id}/catalog",
                 {"itemCategoryId": cat_id, "itemTypeId": type_id}),
                (f"Orders/{order_id}/catalogFilter",
                 {"itemCategoryId": cat_id, "itemTypeId": type_id}),
                (f"Orders/{order_id}/getItemsAvailable",
                 {"itemCategoryId": cat_id, "itemTypeId": type_id}),
                (f"Orders/{order_id}/getItems",
                 {"itemCategoryId": cat_id, "itemTypeId": type_id}),
                (f"Orders/{order_id}/itemsList",
                 {"itemCategoryId": cat_id, "itemTypeId": type_id}),
                (f"Orders/{order_id}/available",
                 {"itemCategoryId": cat_id, "itemTypeId": type_id}),
                # Items standalone
                (f"Items/catalogWholesaler",
                 {"orderId": order_id, "itemCategoryId": cat_id, "itemTypeId": type_id}),
                (f"Items/getWholesalerCatalog",
                 {"orderId": order_id, "itemCategoryId": cat_id, "itemTypeId": type_id}),
                (f"Items/available",
                 {"orderId": order_id, "itemCategoryId": cat_id, "itemTypeId": type_id}),
                (f"Items/catalog",
                 {"orderId": order_id, "itemCategoryId": cat_id, "itemTypeId": type_id}),
                # OrderRows / lignes de commande
                (f"OrderRows/catalog",
                 {"orderId": order_id, "itemCategoryId": cat_id, "itemTypeId": type_id}),
                (f"OrderRows/available",
                 {"orderId": order_id, "itemCategoryId": cat_id, "itemTypeId": type_id}),
            ]

            found_catalog_ep = None
            for ep, params in product_endpoints_v1:
                prods = api.get(ep, params=params)
                if prods and isinstance(prods, list) and prods:
                    p0 = prods[0]
                    print(f"\n  ✅ {ep}")
                    print(f"     {len(prods)} produits — clés : {list(p0.keys())}")
                    print(f"     Exemple : {json.dumps(p0, ensure_ascii=False)[:300]}")
                    found_catalog_ep = ep
                    break
                else:
                    print(f"  ✗  {ep}")

        # Produits — vague 2 : variantes sans itemTypeId (certaines API ignorent ce filtre)
        if type_id and not found_catalog_ep:
            print(f"\n  [3b] Variantes sans itemTypeId…")
            product_endpoints_v2 = [
                (f"Orders/{order_id}/catalog",
                 {"itemCategoryId": cat_id}),
                (f"Orders/{order_id}/getItemsAvailable",
                 {"itemCategoryId": cat_id}),
                (f"Orders/{order_id}/getItems",
                 {"itemCategoryId": cat_id}),
                (f"Items/catalog",
                 {"orderId": order_id, "itemCategoryId": cat_id}),
            ]
            for ep, params in product_endpoints_v2:
                prods = api.get(ep, params=params)
                if prods and isinstance(prods, list) and prods:
                    p0 = prods[0]
                    print(f"\n  ✅ {ep} (sans typeId)")
                    print(f"     {len(prods)} produits — clés : {list(p0.keys())}")
                    print(f"     Exemple : {json.dumps(p0, ensure_ascii=False)[:300]}")
                    found_catalog_ep = ep
                    break
                else:
                    print(f"  ✗  {ep} (sans typeId)")

        # Produits — vague 3 : introspection LoopBack
        if not found_catalog_ep:
            print(f"\n  [3c] Introspection LoopBack — méthodes disponibles sur Orders…")
            meta_endpoints = [
                "Orders",
                f"Orders/{order_id}",
            ]
            for ep in meta_endpoints:
                r = api.get(ep)
                if r:
                    print(f"  ✅ GET {ep} → clés : {list(r.keys()) if isinstance(r, dict) else f'{len(r)} items'}")
                    print(f"     {json.dumps(r if isinstance(r, dict) else r[0], ensure_ascii=False)[:400]}")
                else:
                    print(f"  ✗  GET {ep}")

            print(f"\n  ⚠️  Aucun endpoint catalogue trouvé automatiquement.")
            print(f"  👉 ACTION REQUISE : ouvre le site shop.verdnatura.es dans Chrome,")
            print(f"     navigue jusqu'au catalogue, et dans F12 → Network → XHR/Fetch,")
            print(f"     cherche l'appel GET /api/... qui retourne des produits.")
            print(f"     Copie l'URL complète et partage-la.")

    # ── 4. Nettoyage ──────────────────────────────────────────
    print(f"\n🗑️  [4/4] Suppression commande #{order_id}…")
    ok = api.delete(f"Orders/{order_id}")
    print(f"  {'✅ Supprimée' if ok else '⚠️  Échec suppression — supprimer depuis le site'}")

    print("\n" + "="*54)
    print("  Copie ce log et partage-le pour analyser les endpoints.")
    print("="*54 + "\n")


# ─────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────
def main():
    vn_user, vn_pass = load_credentials()
    now    = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    period = get_period()

    debug_mode = "--debug-api" in sys.argv

    print(f"\n{'='*54}")
    print(f"  🌺 Verdnatura Tracker v7.1 — {now}  [{period}]")
    print(f"{'='*54}")

    if not vn_user or not vn_pass:
        print("❌ Identifiants manquants — remplir config.json")
        sys.exit(1)

    print(f"  👤 {vn_user}")

    if debug_mode:
        debug_api(vn_user, vn_pass)
        sys.exit(0)

    products = scrape_via_api(vn_user, vn_pass)

    if not products:
        print("\n⚠️  Aucun produit récupéré via API REST.")
        print("   Lance : python collect.py --debug-api")
        print("   puis partage le log complet pour analyser l'API.")
        print("\n   En attendant, la dernière collecte réussie reste dans verdnatura.db")
        sys.exit(1)

    products = [p for p in products if p["ref"] and p["name"]]
    print(f"\n✅ {len(products)} produits uniques collectés")

    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    init_db(conn)
    stats = save_snapshot(conn, products, now, period)

    print(f"\n💾 Snapshot [{period}]")
    print(f"   🆕 Nouveaux   : {stats['new']}")
    print(f"   👻 Disparus   : {stats['disappeared']}")
    print(f"   🔄 Réapparus  : {stats['reappeared']}")

    export_json(conn)
    conn.close()
    print(f"\n✅ Terminé [{period}]\n")


if __name__ == "__main__":
    main()
