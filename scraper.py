"""
Feed de productos para Google Merchant Center desde una tienda Shopify.
Configurado para https://gianidafirenze.cl (sirve para cualquier Shopify cambiando la CONFIGURACIÓN).

Formato idéntico al catálogo de referencia de El Ganso:
  un ítem por producto, descripción "Tipo Marca: Título. Talles disponibles: 35, 36.",
  price / sale_price, brand, product_type, condition e identifier_exists = no.

Uso:  pip install requests
      python scraper.py
Salidas: feed.xml (el feed) y feed.json (datos crudos).
"""
import html
import json
import re
import time
from datetime import datetime, timezone
from xml.sax.saxutils import escape

import requests

# ------------------------------------------------------------ CONFIGURACIÓN ---
BASE = "https://gianidafirenze.cl"
MARCA_DEFECTO = "Giani Dafirenze"      # si un producto no tiene "vendor" en Shopify
MONEDA = "CLP"
DECIMALES = 0                          # CLP no usa decimales (COP/USD/ARS: 2)
TITULO_FEED = "Feed de productos Giani Dafirenze"
ARCHIVO_XML = "feed.xml"

DELAY = 0.5
TIMEOUT = 20
MAX_PAGES = 40           # 40 x 250 = 10.000 productos
MIN_RATIO = 0.7          # si hoy hay menos del 70% de los productos de ayer, no se pisa el feed
# -----------------------------------------------------------------------------

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (compatible; ProductFeedBot/1.0)",
    "Accept-Language": "es-CL,es;q=0.9",
})
ESTADOS = {}
INICIO = time.time()


def get(url, retries=6):
    """GET con reintentos; ante 429/503 espera (respeta Retry-After)."""
    for intento in range(retries):
        try:
            r = session.get(url, timeout=TIMEOUT)
            ESTADOS[r.status_code] = ESTADOS.get(r.status_code, 0) + 1
            if r.status_code == 200:
                return r
            if r.status_code == 404:
                return None
            if r.status_code in (429, 403, 503):
                ra = r.headers.get("Retry-After", "")
                pausa = min(int(float(ra)) if re.fullmatch(r"\d+(\.\d+)?", ra) else 8 * (intento + 1), 60)
                print(f"  HTTP {r.status_code} en {url} -> pausa {pausa}s", flush=True)
                time.sleep(pausa)
                continue
        except requests.RequestException as e:
            ESTADOS["error"] = ESTADOS.get("error", 0) + 1
            print(f"  error {type(e).__name__} en {url}", flush=True)
        time.sleep(2 * (intento + 1))
    return None


# ---------- 1. Leer el catálogo ----------

def a_precio(valor, en_centavos=False):
    if valor in (None, ""):
        return None
    try:
        n = float(valor)
    except (TypeError, ValueError):
        return None
    return n / 100 if en_centavos else n


CLAVES_OMITIR = set()               # claves de la ficha que no se quieren en el feed (ej. {"nombre"})


def leer_descripcion(html_desc):
    """Separa la descripción de Shopify en texto y características ('Suela: Goma', 'Altura taco: 6 cm'...)."""
    t = re.sub(r"<\s*(br|/p|/li|/div|/h\d|/tr)[^>]*>", "\n", html_desc or "", flags=re.I)
    t = re.sub(r"<\s*/t[dh]\s*>", ": ", t, flags=re.I)          # tablas: celda clave | celda valor
    t = html.unescape(re.sub(r"<[^>]+>", " ", t))
    texto, caracteristicas, vistos = [], {}, set()
    for linea in t.split("\n"):
        linea = re.sub(r"\s+", " ", linea).strip(" :-•·*\t")
        if not linea or linea.lower() in vistos:
            continue                                     # Shopify a veces repite párrafos
        vistos.add(linea.lower())
        m = re.match(r"^([^:]{2,40}):\s*(.{1,120})$", linea)
        if m and not m.group(2).endswith(":"):
            clave, valor = m.group(1).strip(), m.group(2).strip().rstrip(":").strip()
            if clave.lower() not in CLAVES_OMITIR and valor:
                caracteristicas.setdefault(clave, valor)
        elif linea.lower().rstrip(":") in ("características del producto", "caracteristicas del producto",
                                           "características", "caracteristicas", "detalles"):
            continue
        else:
            texto.append(linea)
    return {"descripcion": "\n\n".join(texto), "caracteristicas": caracteristicas}


def normalizar(p, en_centavos=False):
    imagenes, img_por_variante = [], {}
    for img in p.get("images") or []:
        src = img.get("src") if isinstance(img, dict) else img
        if not src:
            continue
        src = "https:" + src if src.startswith("//") else src
        if src not in imagenes:
            imagenes.append(src)
        for vid in (img.get("variant_ids") or []) if isinstance(img, dict) else []:
            img_por_variante[vid] = src

    variantes = []
    for v in p.get("variants") or []:
        fi = v.get("featured_image")
        img = (fi.get("src") if isinstance(fi, dict) else None) or img_por_variante.get(v.get("id"))
        variantes.append({
            "variant_id": v.get("id"),
            "sku": (v.get("sku") or "").strip(),
            "talle": "" if v.get("title") in (None, "Default Title") else v.get("title").strip(),
            "precio": a_precio(v.get("price"), en_centavos),
            "precio_tachado": a_precio(v.get("compare_at_price"), en_centavos),
            "disponible": bool(v.get("available")),
            "imagen": ("https:" + img if img and img.startswith("//") else img),
        })

    return {
        "product_id": p.get("id"),
        "handle": p.get("handle"),
        "url": f"{BASE}/products/{p.get('handle')}",
        "nombre": re.sub(r"\s+", " ", p.get("title") or "").strip(),
        "marca": (p.get("vendor") or "").strip(),
        "tipo_shopify": (p.get("product_type") or p.get("type") or "").strip(),
        **leer_descripcion(p.get("body_html") or p.get("description") or ""),
        "imagenes": imagenes,
        "variantes": variantes,
    }


def catalogo_products_json():
    productos = []
    for page in range(1, MAX_PAGES + 1):
        r = get(f"{BASE}/products.json?limit=250&page={page}")
        if not r:
            break
        try:
            lote = r.json().get("products", [])
        except ValueError:
            break
        if not lote:
            break
        productos += [normalizar(p) for p in lote]
        print(f"  products.json página {page}: {len(lote)} productos", flush=True)
        time.sleep(DELAY)
    return productos


def catalogo_sitemap():
    """Respaldo si /products.json estuviera bloqueado: sitemap + /products/<handle>.js"""
    handles, pendientes, vistos = set(), [f"{BASE}/sitemap.xml"], set()
    while pendientes:
        sm = pendientes.pop()
        if sm in vistos:
            continue
        vistos.add(sm)
        r = get(sm)
        if not r:
            continue
        for loc in re.findall(r"<loc>\s*(.*?)\s*</loc>", r.text):
            loc = html.unescape(loc)
            if ".xml" in loc and "sitemap" in loc:
                pendientes.append(loc)
            else:
                m = re.match(rf"{re.escape(BASE)}/products/([^/?#]+)$", loc)
                if m:
                    handles.add(m.group(1))
    print(f"Sitemap: {len(handles)} productos", flush=True)
    productos = []
    for h in sorted(handles):
        r = get(f"{BASE}/products/{h}.js")
        time.sleep(DELAY)
        if r:
            try:
                productos.append(normalizar(r.json(), en_centavos=True))
            except ValueError:
                pass
    return productos


# ---------- 2. Feed en el formato de El Ganso ----------

def tipo_producto(p):
    """'Sandalia Mujer / Tortolita Taupe' -> 'Sandalia'; 'Bolso de viaje Mujer / ...' -> 'Bolso de viaje'"""
    antes = re.split(r"\s+(?:Mujer|Hombre|Unisex|Niña|Niño)\b|\s*/", p["nombre"], maxsplit=1)[0].strip()
    if antes and antes != p["nombre"]:
        return antes[:1].upper() + antes[1:]
    t = p["tipo_shopify"]
    return t[:1].upper() + t[1:].lower() if t else "Producto"


def precio_fmt(n):
    return f"{float(n):.{DECIMALES}f} {MONEDA}"


CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def generar_xml(productos):
    items = []
    for p in productos:
        nombre = p["nombre"]
        if not nombre or "GIFT" in nombre.upper() or "TARJETA DE REGALO" in nombre.upper():
            continue
        variantes = [v for v in p["variantes"] if v.get("precio")]
        imagen = next((v["imagen"] for v in variantes if v.get("imagen")), None) or \
                 (p["imagenes"][0] if p["imagenes"] else None)
        if not variantes or not imagen:
            continue  # Google rechaza productos sin precio o sin imagen

        marca = p["marca"] or MARCA_DEFECTO
        tipo = tipo_producto(p)
        disponibles = [v for v in variantes if v["disponible"]]
        ref = disponibles[0] if disponibles else variantes[0]
        precio, tachado = ref["precio"], ref.get("precio_tachado")
        pid = ref["sku"] or str(ref["variant_id"])

        talles = [v["talle"] or "Único" for v in disponibles]
        if talles:
            desc = f"{tipo} {marca}: {nombre}. Talles disponibles: {', '.join(talles)}."
        else:
            desc = f"{tipo} {marca}: {nombre}. Sin talles disponibles por el momento."
        # Igual que en la página: párrafos, y la lista "Características del producto"
        if p.get("descripcion"):
            desc += "\n\n" + p["descripcion"]
        if p.get("caracteristicas"):
            desc += "\n\nCaracterísticas del producto:\n" + "\n".join(
                f"- {k}: {v}" for k, v in p["caracteristicas"].items())
        desc = desc[:5000]

        campos = [
            ("g:id", pid),
            ("g:title", nombre[:150]),
            ("g:description", desc),
            ("g:link", p["url"]),
            ("g:image_link", imagen),
            ("g:availability", "in_stock" if disponibles else "out_of_stock"),
        ]
        if tachado and float(tachado) > float(precio):
            campos += [("g:price", precio_fmt(tachado)), ("g:sale_price", precio_fmt(precio))]
        else:
            campos += [("g:price", precio_fmt(precio))]
        campos += [
            ("g:brand", marca),
            ("g:product_type", tipo),
            ("g:condition", "new"),
            ("g:identifier_exists", "no"),
        ]
        lineas = [f"      <{k}>{escape(CTRL.sub('', str(val)))}</{k}>" for k, val in campos if val]
        items.append("    <item>\n" + "\n".join(lineas) + "\n    </item>")

    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rss xmlns:g="http://base.google.com/ns/1.0" version="2.0">\n'
        "  <channel>\n"
        f"    <title>{escape(TITULO_FEED)}</title>\n"
        f"    <link>{BASE}</link>\n"
        "    <description>Catalogo completo de productos</description>\n"
        + "\n".join(items) +
        "\n  </channel>\n</rss>\n"
    )
    with open(ARCHIVO_XML, "w", encoding="utf-8") as f:
        f.write(xml)
    print(f"{ARCHIVO_XML}: {len(items)} productos")


# ---------- 3. Main ----------

def main():
    try:
        with open("feed.json", encoding="utf-8") as f:
            previos = json.load(f).get("total", 0)
    except (OSError, ValueError):
        previos = 0

    productos = catalogo_products_json()
    if not productos:
        productos = catalogo_sitemap()
    print(f"{len(productos)} productos | estados HTTP: {ESTADOS}", flush=True)

    if not productos or (previos and len(productos) < MIN_RATIO * previos):
        raise SystemExit(f"ERROR: solo {len(productos)} productos (antes {previos}). "
                         "No se actualiza el feed en esta corrida.")

    with open("feed.json", "w", encoding="utf-8") as f:
        json.dump({"generado": datetime.now(timezone.utc).isoformat(), "total": len(productos),
                   "productos": productos}, f, ensure_ascii=False, indent=2)
    generar_xml(productos)
    print(f"Listo en {time.time() - INICIO:.0f}s", flush=True)


if __name__ == "__main__":
    main()
