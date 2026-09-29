#!/bin/bash
set -euo pipefail

# ========= ENV =========
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
set -a
source "$script_dir/../.dataset.env"
set +a

# ========= CSV -> ";" =========
# Los CSV de primas ≥2027 van separados por comas, con BOM y con campos entre comillas
# que contienen comas (Tarifbezeichnung). Un `sed 's/,/;/g'` los rompería: se
# reescriben con el módulo csv (y sin BOM) al separador ";" que espera process.py.
to_semicolon() {
  python3 - "$1" "$2" <<'PY'
import csv, os, sys
src, dst = sys.argv[1], sys.argv[2]
with open(src, newline="", encoding="utf-8-sig") as f:
    head = f.readline(); f.seek(0)
    delim = ";" if head.count(";") > 3 else ","
    rows = list(csv.reader(f, delimiter=delim))
tmp = dst + ".part"
with open(tmp, "w", newline="", encoding="utf-8") as g:
    csv.writer(g, delimiter=";").writerows(rows)
os.replace(tmp, dst)
PY
}

# ========= PARSE ARCHIVES =========
declare -A files
IFS=';' read -ra entries <<< "${DATASET_ARCHIVES:-}"
for entry in "${entries[@]}"; do
  [[ -n "$entry" ]] || continue
  key="${entry%%|*}"
  val="${entry#*|}"
  files["$key"]="$val"
done

last_year="${DATASET_LAST_YEAR:-$(date +%Y)}"
last_url_ch="${DATASET_LAST_URL_CH:-}"
last_url_eu="${DATASET_LAST_URL_EU:-}"

tmp_dir="$script_dir/../datasource/praemien_tmp"
mkdir -p "$tmp_dir"

# ========= CLEAN EXPORT =========
echo "🩹 Cleaning Export directory"
find "$script_dir/../export" -mindepth 1 -not -name '.gitkeep' -exec rm -rf {} + 2>/dev/null || true

# ========= DOWNLOAD ARCHIVES =========
echo "📥 Downloading ZIP files..."
for filename in "${!files[@]}"; do
  url="${files[$filename]}"
  if ! curl -fsSL -o "$tmp_dir/$filename" "$url"; then
    echo "⚠️  Download failed for $filename ($url), skipping"
    rm -f "$tmp_dir/$filename" || true
    continue
  fi
done

# ========= EXTRACT + FIX NAMES =========
echo "📦 Extracting and fixing filenames..."
cd "$tmp_dir"
shopt -s nullglob

for zipfile in *.zip; do
  if ! unzip -qq -t "$zipfile" >/dev/null 2>&1; then
    echo "⚠️  Skipping $zipfile (not a valid ZIP / HTML error)"
    mv "$zipfile" "${zipfile%.zip}.INVALID" || true
    continue
  fi

  year="$(echo "$zipfile" | grep -o '[0-9]\{4\}' || true)"
  [[ -n "$year" ]] || { echo "⚠️  Cannot infer year from $zipfile, skipping"; continue; }

  target_dir="$script_dir/../datasource/$year"
  if [ -d "$target_dir" ]; then
    echo "⚠️  Skipping $year (already exists)"
    continue
  fi

  mkdir -p "$target_dir"
  if ! unzip -qq "$zipfile" -d "$target_dir"; then
    echo "⚠️  Unzip failed for $zipfile, skipping"
    rm -rf "$target_dir"
    continue
  fi

  # Fix filenames
  find "$target_dir" -type f | while IFS= read -r file; do
    base="$(basename "$file")"
    clean_name="$(printf "%s" "$base" \
      | iconv -f ISO-8859-1 -t UTF-8//IGNORE \
      | sed -E 's/Pr[ÄäДд]/Prae/g' \
      | sed 's/[^a-zA-Z0-9_.-]/_/g')"
    clean_name="$(printf "%s" "$clean_name" \
      | sed -E 's/__+/_/g' \
      | sed -E 's/_+\.csv$/.csv/')"
    if [[ "$base" != "$clean_name" ]]; then
      mv "$file" "$(dirname "$file")/$clean_name"
    fi
  done

  # Normalize CSVs (UTF-8 + ;)
  find "$target_dir" -type f -iname "*.csv" | while IFS= read -r csvfile; do
    encoding="$(file -bi "$csvfile" | sed 's/.*charset=//')"
    tmpfile="${csvfile}.tmp"

    if [[ "${encoding,,}" != "utf-8" ]]; then
      iconv -f "$encoding" -t utf-8 "$csvfile" > "$tmpfile" || cp "$csvfile" "$tmpfile"
    else
      cp "$csvfile" "$tmpfile"
    fi

    to_semicolon "$tmpfile" "$csvfile"
    rm -f "$tmpfile"
  done
done

# Cleanup tmp
cd "$script_dir"
rm -rf "$tmp_dir"

# ========= DOWNLOAD RAW CH/EU FOR LAST YEAR =========
echo "📥 Downloading raw CSVs for $last_year..."
year_dir="$script_dir/../datasource/$last_year"
mkdir -p "$year_dir"

for type in CH EU; do
  url_var="last_url_${type,,}"
  url="${!url_var:-}"
  [[ -n "$url" ]] || { echo "⚠️  No URL for $type, skipping"; continue; }

  target_file="$year_dir/Praemien_${type}.csv"
  tmp_file="${target_file}.tmp"

  if ! curl -fsSL -o "$tmp_file" "$url"; then
    echo "⚠️  Download failed for $type ($url), skipping"
    rm -f "$tmp_file" || true
    continue
  fi

  encoding="$(file -bi "$tmp_file" | sed 's/.*charset=//')"
  if [[ "${encoding,,}" != "utf-8" ]]; then
    iconv -f "$encoding" -t utf-8 "$tmp_file" > "$target_file" || cp "$tmp_file" "$target_file"
  else
    cp "$tmp_file" "$target_file"
  fi

  to_semicolon "$target_file" "$target_file"

  rm -f "$tmp_file"
done

# ========= NORMALIZE ALL YEARS + BUILD CONFIG =========
echo "🧹 Normalizing all CSVs and building metadata..."
output_json="$script_dir/../datasource/config.json"
echo '{ "primes": [' > "$output_json"
first=1

# Recorre solo carpetas con 4 dígitos y ordénalas
while IFS= read -r year; do
  [[ -d "$script_dir/../datasource/$year" ]] || continue

  # Busca un CH y un EU si no están con el nombre final
  file_ch="$(find "$script_dir/../datasource/$year" -maxdepth 1 -type f -iname "Pr*CH*.csv" | grep -v "Praemien_CH.csv" | head -n1 || true)"
  file_eu="$(find "$script_dir/../datasource/$year" -maxdepth 1 -type f -iname "Pr*EU*.csv" | grep -v "Praemien_EU.csv" | head -n1 || true)"

  target_ch="$script_dir/../datasource/$year/Praemien_CH.csv"
  target_eu="$script_dir/../datasource/$year/Praemien_EU.csv"

  for src in "$file_ch" "$file_eu"; do

    [[ -f "$src" ]] || continue
    dst="$script_dir/../datasource/$year/$(basename "$src" | sed -E 's/.*CH.*/Praemien_CH.csv/; s/.*EU.*/Praemien_EU.csv/')"

    encoding="$(file -bi "$src" | sed 's/.*charset=//')"
    tmp="${dst}.tmp"

    if [[ "${encoding,,}" != "utf-8" ]]; then
      iconv -f "$encoding" -t utf-8 "$src" > "$tmp" || cp "$src" "$tmp"
    else
      cp "$src" "$tmp"
    fi

    to_semicolon "$tmp" "$dst"
    rm -f "$tmp"

  done

  # si falta alguno, skip año
  [[ -f "$target_ch" && -f "$target_eu" ]] || {
    echo "⚠️  Skipping $year (missing CH or EU CSV)"
    continue
  }

  # añade coma si no es el primero
  if [[ "$first" -eq 0 ]]; then
    echo "," >> "$output_json"
  fi
  first=0

  # paths relativos dentro de datasource
  rel_ch="${year}/Praemien_CH.csv"
  rel_eu="${year}/Praemien_EU.csv"

  echo "  { \"id\": \"l$year\", \"year\": $year, \"path_ch\": \"$rel_ch\", \"path_eu\": \"$rel_eu\", \"encoding_ch\": \"utf-8\", \"encoding_eu\": \"utf-8\", \"sep\":\";\" }" >> "$output_json"
done < <(ls -d "$script_dir/../datasource"/[0-9][0-9][0-9][0-9] 2>/dev/null | xargs -r -n1 basename | sort)

echo "] }" >> "$output_json"
echo "Metadata saved to $output_json"

# ========= BUILD DATASETS =========
echo "📦 Preparing CSV Datasets..."
if python3 -W ignore "$script_dir/process.py"; then
  echo "✅ Done! Datasets ready in build/export directory!"
else
  echo "❌ Error executing process.py"
  exit 1
fi
