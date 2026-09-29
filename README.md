# 🇨🇭 Lamal|Tarmed – Swiss Health Insurance Data Analysis

This project automates the **download**, **standardization**, **database import**, and **data representation** of Switzerland Health insurances related-data from open sources like [opendata.swiss](https://opendata.swiss). It processes all available data o the current year and prepares it for use in analytics or dashboards.

---

## ⚙️ Stack

- 🐍 Python 3.10 + Pipenv
- 🐬 MariaDB 11.3
- 🐳 Docker & Docker Compose
- 📦 CSV-based datasets from [opendata.swiss](https://opendata.swiss)

---

## 🏗️ Project structure

The idea is to have full and useful pipelines inside the "datasets" directory. Each directory contains all the needed instructions for

1. Downloading the needed files / databases for building a dataset
2. Cooking the dataset, mixing and transforming data
3. Loading the dataset into a database

## 🔁 Current pipelines

1. Lamal
2. Tarmed (in progress)

## 🚀 Quickstart (Dockerized)

The easiest way to run everything is using Docker Compose. It handles dataset generation and database provisioning automatically.


### 1. Build and start the system:

#### Launch full stack (Dataset building, )
```bash
docker compose --profile PIPELINE_NAME up -d
```

f.e: `docker compose --profile Lamal up -d`

What this does:
- Downloads all raw data files
- Unzips and cleans them
- Standardizes everything into `.csv` format. You can find the results (the raw CSV Files) in build/export of the pipeline directory
- Starts a MariaDB container
- Imports the data using

---

## ⚗️ Environment Configuration

All important variables are declared in `.dataset.env`. of your pipeline directory. Example:

```.dataset.env
# Dataset archive URLs
export DATASET_ARCHIVES="Archiv_Praemien_2011.zip|https://...;Archiv_Praemien_2012.zip|https://...;..."
export DATASET_LAST_YEAR="2025"
export DATASET_LAST_URL_CH="https://..."
export DATASET_LAST_URL_EU="https://..."
```

On the .env file of the main directory, you can also find some environment variables for configuring docker compose vars. :

```.env
# DB Credentials
MYSQL_ROOT_PASSWORD=root
MYSQL_DATABASE=lamal
MYSQL_USER=lamal
MYSQL_PASSWORD=lamal
```

---

## 🔧 Manual Mode (without Docker)

If you want to build the dataset without Docker, follow this instructions:

### 1. Install dependencies

```bash
sudo apt-get install python3 python3-pip unzip pipenv
# Go to your build dataset directory (cd datasets/Lamal/build)
pip install pipenv (not necessary if pipenv is installed already)
pipenv install
```

### 2. Run the pipeline

```bash
# Go to your build dataset directory (cd datasets/Lamal/build)
pipenv run bash utils/generate_dataset.sh
```

### 3. Launch MariaDB locally and import the data

- Start a local MariaDB/MySQL instance.
- Modify the paths inside CreateAndImportData.sql for pointing to the export directory
  ```bash
  Example: /app/export/assurances.csv'
  ```
- Run the import script manually:
  ```bash
  mysql -u lamal -p lamal < CreateAndImportData.sql
  ```

---

## 🧠 Why preprocess the data?

Swiss federal health data is inconsistent:
- Different encodings (UTF-8, latin-1)
- Column names change over time
- Values and enums are not standardized

This pipeline ensures all years conform to a unified schema for further processing.

---

## 📅 Adding a new premium year to production (RomandeAssure)

The production database (`lamal-db` on frontier) is **not** rebuilt with
`CreateAndImportData.sql` (that script drops every table, and its datadir is an anonymous
Docker volume: never `docker compose down` it). A new year is **added** with
`datasets/Lamal/build/utils/import_year.py`, in one transaction, CH + EU together:

```bash
# on frontier (Python 3.6 + pymysql), once the FOPH has published the year (end of September)
set -a; . /etc/romandeassure/lamal-comparator-api/.env; set +a
python3 import_year.py --year 2028 --regions praemienregionen.xlsx            # dry-run report
python3 import_year.py --year 2028 --regions praemienregionen.xlsx --apply    # write
```

- `praemienregionen.xlsx` = the premium regions of the year (priminfo «Téléchargements»);
  the script only updates communes whose region changed.
- The report fails on any unknown code. The FOPH changed every code for 2027
  (`AKA_03_ERW`, `MIT_UNF`, `PR_REG_1`, `FRA_01_E_0300`, `P_OKPCH`, bare ISO codes in the
  EU file); both encodings are handled, here and in `prime.py`.
- The comparator API resolves `year=latest` as a global `MAX(year)` on every request:
  the site switches as soon as the transaction commits. Rollback:
  `DELETE FROM lamal WHERE year=<year>`.
- Then: bump the CO₂ redistribution table and `LATEST_PREMIUM_YEAR` in
  `romandeassure-widget-lamal-comparator/src/app/flow.js` (FOEN notice, published end of August).

2027 was loaded on 2026-09-29 (219 916 CH + 2 230 EU rows). Normalising the official 2026
file with the same code reproduces the 219 702 production rows of 2026 exactly.
