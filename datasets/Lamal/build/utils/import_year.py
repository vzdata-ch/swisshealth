"""
import_year.py — Añade un año de primas LAMal (CH + EU) a la BD `lamal` de producción.

Es el camino de producción para un año nuevo. `CreateAndImportData.sql` reconstruye
TODO desde cero (DROP de todas las tablas) y en frontier sólo corre sobre un datadir
vacío; los años 2021-2025 entraron con `fix_import_historical.py`, que sólo conoce
la codificación ≤2026. Este script:

  1. lee los CSV oficiales de la OFSP (URL, fichero o ZIP de archivo),
  2. normaliza LAS DOS codificaciones (≤2026: AKL-ERW / MIT-UNF / PR-REG CH1 / FRA-300;
     ≥2027: AKA_03_ERW / MIT_UNF / PR_REG_1 / FRA_01_E_0300 / P_OKPCH) a las
     convenciones que ya tiene la BD,
  3. imprime un informe de validación (códigos desconocidos = error fatal),
  4. con --apply inserta CH + EU en UNA transacción (y, opcionalmente, sincroniza la
     tabla `region` con las regiones de primas oficiales del año).

Convenciones de la BD (verificadas: normalizando el CSV oficial 2026 se reproducen
las 219 702 filas 2026 de producción sin una sola diferencia):
  - pays      CH / EU
  - canton    CH tal cual (incluye ZE y ZR); EU = "EU " + código ISO ("EU AT")
  - region    entero 0-3
  - age3      KIN / JUG / ERW
  - age       Altersuntergruppe (K1..K5, J1, E1); vacío -> J1 / E1 según age3
  - accident  1 = MIT_UNF, 0 = OHN_UNF
  - franchise entero; EU siempre 300 (convención histórica, niños incluidos)
  - isBaseP   CH: 1 SOLO para la tarifa BASE con accidente (en el CSV 2027 la OFSP
              lo pone también sin accidente); EU: tal cual (1)
  - tarifTyp  TAR-BASE / TAR-HAM / TAR-HMO / TAR-DIV. Desde 2027 la OFSP clasifica
              BASE / PRAXIS / FLEX / TEL_DIG / PHARM: se conserva la clase del año
              anterior para la misma (aseguradora, tarifa) y, para tarifas nuevas,
              PRAXIS -> TAR-HMO si el nombre dice HMO/Gesundheitspraxis, si no TAR-HAM;
              FLEX / TEL_DIG / PHARM -> TAR-DIV
  - tarifDesc CH BASE = "Grundversicherung" (el CSV 2027 dice "BASE")

La API (lamal-comparator-api) sólo lee canton, region, age, accident, franchise,
year, prime y assuranceId, y resuelve year="latest" como MAX(year) GLOBAL en cada
petición: una sola fila del año nuevo bascula todo el sitio. Por eso CH y EU entran
en la misma transacción, y sin el prefijo "EU " las filas AT/BE/FR/GR/LU se
mezclarían con los cantones BE/FR/GR/LU.

Compatible con Python 3.6 (frontier). Uso en frontier:

  set -a; . /etc/romandeassure/lamal-comparator-api/.env; set +a
  python3 import_year.py --year 2027                      # dry-run: sólo informe
  python3 import_year.py --year 2027 --regions praemienregionen.xlsx   # + regiones
  python3 import_year.py --year 2027 --regions praemienregionen.xlsx --apply

Sin BD (validación local): --no-db [--prev-zip Archiv_Praemien_2026.zip]
"""

import argparse
import collections
import csv
import io
import os
import re
import sys
import urllib.request
import zipfile
import xml.etree.ElementTree as ET

URL_CH = "https://opendata.bagnet.ch/?r=/download&path=L1ByYWVtaWVuL1Byw6RtaWVuX0NILmNzdg%3D%3D"
URL_EU = "https://opendata.bagnet.ch/?r=/download&path=L1ByYWVtaWVuL1Byw6RtaWVuX0VVLmNzdg%3D%3D"
ROMAND = ["GE", "VD", "FR", "NE", "VS", "JU", "BE"]

PAYS = {"CH": "CH", "EU": "EU", "P_OKPCH": "CH", "P_OKPEU": "EU"}
AGE3 = {
    "AKL-KIN": "KIN", "AKL-JUG": "JUG", "AKL-ERW": "ERW",
    "AKA_01_KIN": "KIN", "AKA_02_JUG": "JUG", "AKA_03_ERW": "ERW",
}
ACCIDENT = {"MIT-UNF": 1, "OHN-UNF": 0, "MIT_UNF": 1, "OHN_UNF": 0}
SUBGROUP_DEFAULT = {"JUG": "J1", "ERW": "E1"}
VALID_AGE = {"K1", "K2", "K3", "K4", "K5", "J1", "E1"}
REGION_RE = re.compile(r"^PR[-_]REG[ _](?:CH|EU)?(\d)$")
FRANCHISE_RE = re.compile(r"^FRA[-_](?:\d+_[EJK]_)?0*(\d+)$")
LEGACY_TYP = {"TAR-BASE", "TAR-HAM", "TAR-HMO", "TAR-DIV"}
NEW_TYP = {"BASE", "PRAXIS", "FLEX", "TEL_DIG", "PHARM"}
HMO_RE = re.compile(r"HMO|GESUNDHEITSPRAXIS", re.I)

INSERT_SQL = (
    "INSERT INTO lamal (id, year, assuranceId, canton, pays, region, age3, accident, "
    "franchise, prime, isBaseP, isBaseF, age, tarifDesc, tarifTyp, tarif) "
    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
)


class ImportErrorFatal(Exception):
    pass


# ----------------------------------------------------------------------------- lectura

def _fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (swisshealth import_year)"})
    with urllib.request.urlopen(req, timeout=300) as resp:
        return resp.read()


def _decode(raw):
    for enc in ("utf-8-sig", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            pass
    raise ImportErrorFatal("encoding desconocido")


def _rows_from_text(txt):
    first = txt.split("\n", 1)[0]
    sep = ";" if first.count(";") > 3 else ","
    return list(csv.DictReader(io.StringIO(txt), delimiter=sep))


def load_csv(src):
    """src: URL, ruta a CSV, o 'ruta.zip:CH' / 'ruta.zip:EU' (archivo anual de la OFSP)."""
    if src.startswith("http://") or src.startswith("https://"):
        raw = _fetch(src)
    elif re.search(r"\.zip:(CH|EU)$", src):
        path, kind = src.rsplit(":", 1)
        z = zipfile.ZipFile(path)
        pat = re.compile(r"mien_%s\.csv$" % kind)
        name = next((n for n in z.namelist() if pat.search(n)), None)
        if name is None:
            raise ImportErrorFatal("%s: no hay Prämien_%s.csv en %s" % (src, kind, z.namelist()))
        raw = z.read(name)
    else:
        with open(src, "rb") as fh:
            raw = fh.read()
    txt = _decode(raw)
    if len(txt) < 1000:
        raise ImportErrorFatal("%s: el CSV solo trae la cabecera (%d bytes)" % (src, len(txt)))
    return _rows_from_text(txt)


# ------------------------------------------------------------------------ normalización

def classify_tariff(typ, tarif, desc, prev_map, assurance_id):
    """Devuelve la clase TAR-* histórica."""
    if typ in LEGACY_TYP:
        return typ
    if typ not in NEW_TYP:
        raise ImportErrorFatal("Tariftyp desconocido: %r" % typ)
    if typ == "BASE":
        return "TAR-BASE"
    known = prev_map.get((assurance_id, tarif))
    if known and known != "TAR-BASE":
        return known
    if typ == "PRAXIS":
        return "TAR-HMO" if (HMO_RE.search(tarif or "") or HMO_RE.search(desc or "")) else "TAR-HAM"
    return "TAR-DIV"


def normalize(r, eu, prev_map, unknown):
    """Una fila del CSV oficial -> tupla en el orden de INSERT_SQL (sin id ni year)."""
    def need(table, key, label):
        if key not in table:
            unknown[label + ":" + key] += 1
            return None
        return table[key]

    pays = need(PAYS, r.get("Hoheitsgebiet", ""), "Hoheitsgebiet")
    canton = (r.get("Land") if eu else r.get("Kanton")) or ""
    if pays == "EU" and not canton.startswith("EU "):
        canton = "EU " + canton
    m = REGION_RE.match(r.get("Region", ""))
    if not m:
        unknown["Region:" + r.get("Region", "")] += 1
    region = int(m.group(1)) if m else None
    age3 = need(AGE3, r.get("Altersklasse", ""), "Altersklasse")
    accident = need(ACCIDENT, r.get("Unfalleinschluss", ""), "Unfalleinschluss")
    m = FRANCHISE_RE.match(r.get("Franchise", ""))
    if not m:
        unknown["Franchise:" + r.get("Franchise", "")] += 1
    franchise = int(m.group(1)) if m else None
    if pays == "EU":
        franchise = 300
    age = r.get("Altersuntergruppe") or SUBGROUP_DEFAULT.get(age3, age3)
    if age not in VALID_AGE:
        unknown["Altersuntergruppe:" + str(age)] += 1
    try:
        prime = float(r.get("Prämie") or r.get("Prime") or "")
    except ValueError:
        unknown["Prämie:" + str(r.get("Prämie"))] += 1
        prime = None
    typ = r.get("Tariftyp", "")
    tarif = r.get("Tarif", "")
    desc = r.get("Tarifbezeichnung", "")
    try:
        assurance_id = int(r.get("Versicherer", ""))
    except ValueError:
        unknown["Versicherer:" + r.get("Versicherer", "")] += 1
        assurance_id = None
    try:
        tartyp = classify_tariff(typ, tarif, desc, prev_map, assurance_id)
    except ImportErrorFatal:
        unknown["Tariftyp:" + typ] += 1
        tartyp = None
    is_base = tartyp == "TAR-BASE"
    is_base_p = int(r.get("isBaseP") or 0)
    if pays == "CH":
        is_base_p = 1 if (is_base and accident == 1) else 0
    is_base_f = int(r.get("isBaseF") or 0)
    if pays == "CH" and is_base and desc == "BASE":
        desc = "Grundversicherung"
    return (assurance_id, canton, pays, region, age3, accident, franchise, prime,
            is_base_p, is_base_f, age, desc, tartyp, tarif)


def build_records(rows_ch, rows_eu, prev_map):
    unknown = collections.Counter()
    out = []
    for rows, eu in ((rows_ch, False), (rows_eu, True)):
        for r in rows:
            out.append(normalize(r, eu, prev_map, unknown))
    return out, unknown


# ----------------------------------------------------------------------------- regiones

def read_premium_regions(path):
    """Hoja A_COM de praemienregionen.xlsx (priminfo): {No OFS: (canton, region, commune)}."""
    ns_m = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    ns_r = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
    z = zipfile.ZipFile(path)
    names = set(z.namelist())
    shared = []
    if "xl/sharedStrings.xml" in names:
        for si in ET.fromstring(z.read("xl/sharedStrings.xml")).findall(ns_m + "si"):
            shared.append("".join(t.text or "" for t in si.iter(ns_m + "t")))
    wb = ET.fromstring(z.read("xl/workbook.xml"))
    rels = {r.get("Id"): r.get("Target") for r in ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))}
    sheet = [s for s in wb.iter(ns_m + "sheet") if s.get("name") == "A_COM"]
    if not sheet:
        raise ImportErrorFatal("%s: no hay hoja A_COM" % path)
    target = rels[sheet[0].get(ns_r + "id")]
    for cand in (target, target.lstrip("/"), "xl/" + target.lstrip("/")):
        if cand in names:
            target = cand
            break
    regions = {}
    for row in ET.fromstring(z.read(target)).iter(ns_m + "row"):
        vals = {}
        for c in row.findall(ns_m + "c"):
            col = re.match(r"[A-Z]+", c.get("r")).group(0)
            v, inline = c.find(ns_m + "v"), c.find(ns_m + "is")
            if inline is not None:
                vals[col] = "".join(t.text or "" for t in inline.iter(ns_m + "t"))
            elif v is not None:
                vals[col] = shared[int(v.text)] if c.get("t") == "s" else v.text
        ofs, reg = vals.get("A", ""), vals.get("D", "")
        if ofs.isdigit() and reg.isdigit():
            regions[int(ofs)] = (vals.get("B", ""), int(reg), vals.get("C", ""))
    if len(regions) < 1500:
        raise ImportErrorFatal("%s: A_COM trae solo %d municipios" % (path, len(regions)))
    return regions


def plan_region_sync(cur, official):
    """Cambios mínimos en `region`: actualizar municipios existentes cuya región cambió e
    insertar los municipios multi-región que `communes` referencia y `region` no tiene.
    Los OFS que ya no existen (fusiones) se conservan: `communes` puede seguir usándolos."""
    cur.execute("SELECT ofsId, canton, region FROM region")
    current = {int(o): (c, int(r)) for o, c, r in cur.fetchall()}
    cur.execute("SELECT DISTINCT ofsId FROM communes")
    referenced = {int(o) for (o,) in cur.fetchall()}
    multi = {c for c, reg, _ in official.values() if reg != 0}
    updates, inserts = [], []
    for ofs, (canton, reg, name) in sorted(official.items()):
        if ofs in current:
            if current[ofs][1] != reg:
                updates.append((reg, ofs, current[ofs][1], canton, name))
        elif canton in multi and ofs in referenced:
            inserts.append((canton, ofs, name, reg))
    return updates, inserts


# ---------------------------------------------------------------------------- informe

def report(records, unknown, year, prev_counts):
    ok = True
    print("\n=== Informe %d ===" % year)
    by_pays = collections.Counter(r[2] for r in records)
    print("filas: %s" % dict(by_pays))
    if unknown:
        ok = False
        print("CÓDIGOS DESCONOCIDOS (fatal): %s" % dict(unknown.most_common(20)))
    for label, idx in (("age", 10), ("accident", 5), ("franchise", 6), ("isBaseP", 8),
                       ("isBaseF", 9), ("tarifTyp", 12), ("region", 3), ("age3", 4)):
        c = collections.Counter((r[2], r[idx]) for r in records)
        print("  %-9s %s" % (label, dict(sorted(c.items(), key=lambda kv: str(kv[0])))))
    eu_bad = [r for r in records if r[2] == "EU" and not r[1].startswith("EU ")]
    if eu_bad:
        ok = False
        print("FILAS EU SIN PREFIJO: %d" % len(eu_bad))
    if prev_counts:
        for pays, n in sorted(by_pays.items()):
            prev = prev_counts.get(pays)
            if prev:
                ratio = n / float(prev)
                flag = "" if 0.85 <= ratio <= 1.15 else "   <-- REVISAR"
                print("  %s: %d filas vs %d el año anterior (x%.3f)%s" % (pays, n, prev, ratio, flag))
                if flag:
                    ok = False
    print("\nCaso de referencia (adulto E1, franquicia 300, con accidente, BASE):")
    for canton in ROMAND:
        vals = [r[7] for r in records if r[1] == canton and r[10] == "E1" and r[6] == 300
                and r[5] == 1 and r[8] == 1]
        if vals:
            print("  %s n=%-3d min %8.2f  max %8.2f  media %8.2f"
                  % (canton, len(vals), min(vals), max(vals), sum(vals) / len(vals)))
        else:
            ok = False
            print("  %s SIN FILAS" % canton)
    return ok


# ------------------------------------------------------------------------------- main

def prev_map_from_rows(rows_ch):
    unknown = collections.Counter()
    mapping = {}
    for r in rows_ch:
        t = normalize(r, False, {}, unknown)
        mapping[(t[0], t[13])] = t[12]
    return mapping


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--year", type=int, required=True)
    ap.add_argument("--ch", default=URL_CH, help="URL, CSV o 'archivo.zip:CH'")
    ap.add_argument("--eu", default=URL_EU, help="URL, CSV o 'archivo.zip:EU'")
    ap.add_argument("--regions", help="praemienregionen.xlsx del año (priminfo) para sincronizar `region`")
    ap.add_argument("--apply", action="store_true", help="escribir en la BD (por defecto: solo informe)")
    ap.add_argument("--replace", action="store_true", help="borrar antes las filas existentes de ese año")
    ap.add_argument("--no-db", action="store_true", help="validación local sin BD")
    ap.add_argument("--prev-zip", help="con --no-db: archivo ZIP del año anterior para la clase de tarifa")
    ap.add_argument("--out-csv", help="escribir también las filas normalizadas")
    ap.add_argument("--host", default=os.getenv("DB_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.getenv("DB_PORT", "3306")))
    ap.add_argument("--user", default=os.getenv("DB_USER", "lamal"))
    ap.add_argument("--password", default=os.getenv("DB_PASS", ""))
    ap.add_argument("--database", default=os.getenv("DB_NAME", "lamal"))
    a = ap.parse_args()
    if a.apply and a.no_db:
        ap.error("--apply necesita la BD")

    print("Leyendo CH: %s" % a.ch)
    rows_ch = load_csv(a.ch)
    print("Leyendo EU: %s" % a.eu)
    rows_eu = load_csv(a.eu)
    years = {r.get("Geschäftsjahr") for r in rows_ch + rows_eu if r.get("Geschäftsjahr")}
    if years and years != {str(a.year)}:
        raise ImportErrorFatal("los CSV son del año %s, no %d" % (sorted(years), a.year))

    db = cur = None
    prev_map, prev_counts = {}, {}
    if not a.no_db:
        import pymysql  # solo hace falta con BD
        db = pymysql.connect(host=a.host, port=a.port, user=a.user, password=a.password,
                             database=a.database, autocommit=False, charset="utf8mb4")
        cur = db.cursor()
        cur.execute("SELECT DISTINCT assuranceId, tarif, tarifTyp FROM lamal "
                    "WHERE year=%s AND pays='CH'", (a.year - 1,))
        prev_map = {(int(x), t): typ for x, t, typ in cur.fetchall()}
        cur.execute("SELECT pays, COUNT(*) FROM lamal WHERE year=%s GROUP BY pays", (a.year - 1,))
        prev_counts = {p: n for p, n in cur.fetchall()}
    elif a.prev_zip:
        prev_map = prev_map_from_rows(load_csv(a.prev_zip + ":CH"))

    records, unknown = build_records(rows_ch, rows_eu, prev_map)
    ok = report(records, unknown, a.year, prev_counts)

    if a.out_csv:
        with open(a.out_csv, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["assuranceId", "canton", "pays", "region", "age3", "accident", "franchise",
                        "prime", "isBaseP", "isBaseF", "age", "tarifDesc", "tarifTyp", "tarif"])
            w.writerows(records)
        print("Filas normalizadas en %s" % a.out_csv)

    region_updates, region_inserts = [], []
    if a.regions:
        official = read_premium_regions(a.regions)
        print("\nRegiones oficiales: %d municipios en A_COM" % len(official))
        if cur is not None:
            region_updates, region_inserts = plan_region_sync(cur, official)
            for reg, ofs, old, canton, name in region_updates:
                print("  UPDATE region OFS %d %s (%s): %d -> %d" % (ofs, name, canton, old, reg))
            for canton, ofs, name, reg in region_inserts:
                print("  INSERT region OFS %d %s (%s): %d" % (ofs, name, canton, reg))
            if not region_updates and not region_inserts:
                print("  sin cambios")

    if not ok:
        print("\nVALIDACIÓN FALLIDA: no se escribe nada.")
        return 2
    if not a.apply:
        print("\nDry-run: nada escrito. Repetir con --apply.")
        return 0

    cur.execute("SELECT COUNT(*) FROM lamal WHERE year=%s", (a.year,))
    existing = cur.fetchone()[0]
    if existing and not a.replace:
        print("\nYa hay %d filas de %d. Usa --replace para sustituirlas." % (existing, a.year))
        return 3
    try:
        if existing:
            cur.execute("DELETE FROM lamal WHERE year=%s", (a.year,))
            print("Borradas %d filas previas de %d" % (cur.rowcount, a.year))
        cur.execute("SELECT COALESCE(MAX(id), -1) + 1 FROM lamal")
        next_id = int(cur.fetchone()[0])
        batch = []
        for i, rec in enumerate(records):
            batch.append((next_id + i, a.year) + rec)
            if len(batch) == 5000:
                cur.executemany(INSERT_SQL, batch)
                batch = []
        if batch:
            cur.executemany(INSERT_SQL, batch)
        for reg, ofs, _old, _canton, _name in region_updates:
            cur.execute("UPDATE region SET region=%s WHERE ofsId=%s", (reg, ofs))
        if region_inserts:
            cur.execute("SELECT COALESCE(MAX(id), -1) + 1 FROM region")
            rid = int(cur.fetchone()[0])
            for j, (canton, ofs, name, reg) in enumerate(region_inserts):
                cur.execute("INSERT INTO region (id, canton, ofsId, communaute, region) "
                            "VALUES (%s,%s,%s,%s,%s)", (rid + j, canton, ofs, name[:27], reg))
        db.commit()
    except Exception:
        db.rollback()
        print("ERROR: rollback, la BD queda como estaba.")
        raise
    cur.execute("SELECT pays, COUNT(*) FROM lamal WHERE year=%s GROUP BY pays", (a.year,))
    print("\n✅ %d importado: %s" % (a.year, dict(cur.fetchall())))
    cur.execute("SELECT MAX(year) FROM lamal")
    print("MAX(year) = %s" % cur.fetchone()[0])
    db.close()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ImportErrorFatal as exc:
        print("ERROR: %s" % exc)
        sys.exit(1)
