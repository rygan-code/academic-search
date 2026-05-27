# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Downloads a paper given its DOI or arXiv ID.

Resolves metadata via Crossref/OpenAlex/arXiv to name the file as 'Year-Author-Title.pdf'.
Tries multiple download channels: direct OA links, and fallback to Sci-Hub mirrors.
Saves to E:\\literature database\\pdf, falling back to C:\\Users\\52402\\literature database\\pdf.
"""

# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "python-dotenv",
# ]
# ///

import argparse
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import shutil
import datetime
from typing import Optional, Tuple

# Ensure we can load science_skills.science_skills_common from the script directory
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
  sys.path.append(SCRIPT_DIR)

from science_skills.science_skills_common import http_client

# Constants
DEFAULT_DB_DIR = r"E:\literature database"
FALLBACK_DB_DIR = r"C:\Users\52402\literature database"
SCI_HUB_MIRRORS = ["https://sci-hub.se", "https://sci-hub.st", "https://sci-hub.ru"]
MIN_PDF_SIZE_BYTES = 10240  # 10 KB

# Clients with fixed base URLs
_CROSSREF_CLIENT = http_client.HttpClient("https://api.crossref.org/", qps=2.0, user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
_OPENALEX_CLIENT = http_client.HttpClient("https://api.openalex.org/", qps=2.0, user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
_ARXIV_CLIENT = http_client.HttpClient("http://export.arxiv.org/", qps=1.0 / 3.0, user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


def sanitize_filename(name: str) -> str:
  """Removes/replaces characters that are illegal in Windows filenames."""
  # Replace characters that are invalid in Windows: \ / : * ? " < > |
  sanitized = re.sub(r'[\\/:*?"<>|]', "_", name)
  # Collapse spaces/newlines
  sanitized = re.sub(r"\s+", " ", sanitized).strip()
  # Limit length to prevent path too long errors (200 chars max)
  return sanitized[:200]


def get_first_author_lastname(authors_list: list) -> str:
  """Extracts the first author's family name or display name."""
  if not authors_list:
    return "Unknown"
  first_author = authors_list[0]
  if isinstance(first_author, dict):
    family = first_author.get("family") or first_author.get("author", {}).get("display_name")
    if family:
      return family
  elif isinstance(first_author, str):
    # If it is a string like "Smith JS" or "John Smith", get the last word
    parts = first_author.split()
    if parts:
      return parts[-1]
  return "Unknown"


def fetch_crossref_metadata(doi: str) -> Optional[Tuple[str, str, str]]:
  """Queries Crossref to get (year, first_author, title)."""
  url = f"works/{urllib.parse.quote(doi)}"
  try:
    data = _CROSSREF_CLIENT.fetch_json(url)
    message = data.get("message", {})
    title = message.get("title", [""])[0]
    
    # Extract Year
    year = "Unknown"
    created = message.get("created", {})
    date_parts = created.get("date-parts", [[]])[0]
    if date_parts:
      year = str(date_parts[0])
    else:
      issued = message.get("issued", {})
      date_parts = issued.get("date-parts", [[]])[0]
      if date_parts:
        year = str(date_parts[0])
        
    authors = message.get("author", [])
    author = get_first_author_lastname(authors)
    
    if title and author and year:
      return year, author, title
  except Exception as e:
    print(f"Diagnostics: Crossref metadata query failed: {e}", file=sys.stderr)
  return None


def fetch_openalex_metadata(doi: str) -> Optional[Tuple[str, str, str]]:
  """Queries OpenAlex to get (year, first_author, title)."""
  url = f"https://api.openalex.org/works/https://doi.org/{urllib.parse.quote(doi)}"
  try:
    data = _OPENALEX_CLIENT.fetch_json(url)
    title = data.get("title", "")
    year = str(data.get("publication_year", "Unknown"))
    
    # Extract first author display name (or extract family name)
    authorships = data.get("authorships", [])
    author = "Unknown"
    if authorships:
      display_name = authorships[0].get("author", {}).get("display_name", "")
      if display_name:
        author = display_name.split()[-1]  # get last name
        
    if title and author and year:
      return year, author, title
  except Exception as e:
    print(f"Diagnostics: OpenAlex metadata query failed: {e}", file=sys.stderr)
  return None


def fetch_arxiv_metadata(arxiv_id: str) -> Optional[Tuple[str, str, str]]:
  """Queries arXiv API to get (year, first_author, title)."""
  url = f"api/query?id_list={arxiv_id}"
  try:
    xml_data = _ARXIV_CLIENT.fetch_bytes(url)
    root = ET.fromstring(xml_data)
    
    # arXiv namespace
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    entry = root.find("atom:entry", ns)
    if entry is not None:
      title_elem = entry.find("atom:title", ns)
      title = title_elem.text.replace("\n", " ").strip() if title_elem is not None else ""
      
      published_elem = entry.find("atom:published", ns)
      year = published_elem.text[:4] if published_elem is not None and published_elem.text else "Unknown"
      
      authors = []
      for author_elem in entry.findall("atom:author", ns):
        name_elem = author_elem.find("atom:name", ns)
        if name_elem is not None and name_elem.text:
          authors.append(name_elem.text)
          
      author = get_first_author_lastname(authors)
      if title and author and year:
        return year, author, title
  except Exception as e:
    print(f"Diagnostics: arXiv metadata query failed: {e}", file=sys.stderr)
  return None


def get_target_db_dir(db_dir: str) -> str:
  """Finds or creates database root folder with fallback support."""
  try:
    os.makedirs(db_dir, exist_ok=True)
    # Double check write permission
    test_file = os.path.join(db_dir, ".write_test")
    with open(test_file, "w") as f:
      f.write("test")
    os.remove(test_file)
    return db_dir
  except Exception:
    print(f"Diagnostics: Directory {db_dir} is not writable. Falling back to {FALLBACK_DB_DIR}.", file=sys.stderr)
    os.makedirs(FALLBACK_DB_DIR, exist_ok=True)
    return FALLBACK_DB_DIR


def find_identifier_in_index(index_path: str, file_base: str) -> Optional[str]:
  """Tries to find a paper's identifier in the existing index.md."""
  if not os.path.exists(index_path):
    return None
  try:
    with open(index_path, "r", encoding="utf-8") as f:
      for line in f:
        if "|" in line:
          parts = [p.strip() for p in line.split("|")]
          if len(parts) >= 5:
            y, auth, t = parts[1], parts[2], parts[3]
            row_file_base = sanitize_filename(f"{y}-{auth}-{t}")
            row_id = parts[4]
            row_id_clean = row_id.lower().replace("arxiv:", "").replace("doi:", "")
            clean_file_base = file_base.lower().replace("arxiv:", "").replace("doi:", "")
            
            if row_file_base.lower() == file_base.lower():
              return row_id
            if clean_file_base and (clean_file_base in row_id_clean or row_id_clean in clean_file_base):
              return row_id
  except Exception:
    pass
  return None


def update_index_file(index_path: str, year: str, author: str, title: str, identifier: str, status: str, file_base: str, summary: str = "-"):
  """Creates or updates the index.md file in the database root with the paper entry."""
  now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
  
  # Ensure target directory exists
  os.makedirs(os.path.dirname(index_path), exist_ok=True)
  
  header = (
      "# 文献库索引 (Literature Database Index)\n\n"
      "| 年份 (Year) | 作者 (Author) | 标题 (Title) | 标识符 (Identifier) | 状态 (Status) | 简介 (Summary) | 更新时间 (Updated At) |\n"
      "|---|---|---|---|---|---|---|\n"
  )
  
  lines = []
  if os.path.exists(index_path):
    try:
      with open(index_path, "r", encoding="utf-8") as f:
        lines = f.readlines()
    except Exception as e:
      print(f"Diagnostics: Warning, failed to read existing index.md: {e}", file=sys.stderr)
  
  if not lines or not any("| 年份 (Year)" in line for line in lines):
    lines = header.splitlines(keepends=True)
  
  def clean_col(val: str) -> str:
    return str(val).replace("|", "\\|").replace("\n", " ").replace("\r", "").strip()
  
  year_c = clean_col(year)
  author_c = clean_col(author)
  title_c = clean_col(title)
  identifier_c = clean_col(identifier)
  status_c = clean_col(status)
  summary_c = clean_col(summary)
  
  new_row = f"| {year_c} | {author_c} | {title_c} | {identifier_c} | {status_c} | {summary_c} | {now_str} |\n"
  
  entry_index = -1
  norm_id = identifier.lower().replace("arxiv:", "").replace("doi:", "") if identifier else ""
  norm_file_base = file_base.lower().replace("arxiv:", "").replace("doi:", "")
  
  for i, line in enumerate(lines):
    if "|" in line:
      parts = [p.strip() for p in line.split("|")]
      if len(parts) >= 5:
        row_id = parts[4].lower()
        row_id_clean = row_id.replace("arxiv:", "").replace("doi:", "")
        y, auth, t = parts[1], parts[2], parts[3]
        row_file_base = sanitize_filename(f"{y}-{auth}-{t}").lower()
        
        # Check matching
        match = False
        if identifier != "Unknown" and row_id == identifier.lower():
          match = True
        elif norm_id and (norm_id in row_id_clean or row_id_clean in norm_id):
          match = True
        elif row_file_base == file_base.lower():
          match = True
        elif norm_file_base and (norm_file_base in row_id_clean or row_id_clean in norm_file_base):
          match = True
          
        if match:
          entry_index = i
          break
  
  if entry_index != -1:
    old_parts = [p.strip() for p in lines[entry_index].split("|")]
    
    # Preserve metadata fields if they were unknown in the new update but resolved in the old row
    if year_c == "Unknown" and len(old_parts) >= 2 and old_parts[1] != "Unknown":
      year_c = old_parts[1]
    if author_c == "Unknown" and len(old_parts) >= 3 and old_parts[2] != "Unknown":
      author_c = old_parts[2]
    if (title_c == "Paper" or title_c == "Unknown" or title_c == file_base) and len(old_parts) >= 4 and old_parts[3] != "Unknown" and old_parts[3] != "Paper":
      title_c = old_parts[3]
    if identifier_c == "Unknown" and len(old_parts) >= 5 and old_parts[4] != "Unknown":
      identifier_c = old_parts[4]
      
    if (summary_c == "-" or not summary_c) and len(old_parts) >= 7:
      old_summary = old_parts[6]
      if old_summary and old_summary != "-":
        summary_c = old_summary
        
    lines[entry_index] = f"| {year_c} | {author_c} | {title_c} | {identifier_c} | {status_c} | {summary_c} | {now_str} |\n"
    print(f"Diagnostics: Updated existing index entry for {identifier_c}.", file=sys.stderr)
  else:
    if lines and not lines[-1].endswith("\n"):
      lines[-1] += "\n"
    lines.append(new_row)
    print(f"Diagnostics: Added new index entry for {identifier_c}.", file=sys.stderr)
  
  try:
    with open(index_path, "w", encoding="utf-8") as f:
      f.writelines(lines)
  except Exception as e:
    print(f"Diagnostics: Error writing to index.md: {e}", file=sys.stderr)


def validate_pdf_content(content: bytes) -> bool:
  """Verifies the content starts with %PDF- and is above the minimum size."""
  if not content.startswith(b"%PDF-"):
    return False
  if len(content) < MIN_PDF_SIZE_BYTES:
    return False
  return True


def fetch_generic_url_bytes(url: str, timeout: float = 30.0, extra_headers: Optional[dict] = None) -> bytes:
  """Fetches raw bytes from a generic URL, bypassing SSL verification for Sci-Hub compatibility."""
  import ssl
  import gzip
  parsed = urllib.parse.urlparse(url)
  # Respect the rate limiter for the domain
  limiter = http_client._RateLimiter(parsed.netloc, qps=1.0)
  limiter.wait()
  
  headers = {
      "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
      "Accept-Encoding": "gzip"
  }
  if extra_headers:
    headers.update(extra_headers)
    
  req = urllib.request.Request(url, headers=headers)
  
  # Use custom SSL context to disable verification
  ctx = ssl.create_default_context()
  ctx.check_hostname = False
  ctx.verify_mode = ssl.CERT_NONE
  
  # urllib does not automatically follow protocol-relative redirects well, or might throw HTTPError.
  # We will catch HTTPError but let other network exceptions propagate.
  try:
    with urllib.request.urlopen(req, context=ctx, timeout=timeout) as response:
      content = response.read()
      # Check if gzipped
      if response.headers.get("Content-Encoding", "").lower() in ("gzip", "x-gzip"):
        content = gzip.decompress(content)
      return content
  except urllib.error.HTTPError as exc:
    # Read the error body so we can include it in the exception or debug logs
    error_body = exc.read()
    exc.close()
    raise http_client.HttpError(
        f"HTTP Error {exc.code} while fetching {url}",
        status_code=exc.code,
        body=error_body,
        url=url
    ) from exc


def fetch_generic_url_text(url: str, timeout: float = 30.0) -> str:
  """Fetches text content from a generic URL, bypassing SSL verification."""
  content_bytes = fetch_generic_url_bytes(url, timeout=timeout)
  return content_bytes.decode("utf-8", errors="replace")



def try_download_direct_oa(doi: str) -> Optional[bytes]:
  """Attempts to find direct OA link via OpenAlex and download PDF."""
  url = f"https://api.openalex.org/works/https://doi.org/{urllib.parse.quote(doi)}"
  try:
    data = _OPENALEX_CLIENT.fetch_json(url)
    best_loc = data.get("best_oa_location")
    if best_loc and best_loc.get("pdf_url"):
      pdf_url = best_loc["pdf_url"]
      print(f"Diagnostics: Trying direct OA link: {pdf_url}", file=sys.stderr)
      content = fetch_generic_url_bytes(pdf_url, timeout=30)
      if validate_pdf_content(content):
        return content
  except Exception as e:
    print(f"Diagnostics: Direct OA download failed: {e}", file=sys.stderr)
  return None


def fetch_dynamic_scihub_mirrors() -> list:
  """Dynamically fetches active Sci-Hub mirrors from https://sci-hub.now.sh/."""
  mirrors = []
  url = "https://sci-hub.now.sh/"
  print(f"Diagnostics: Dynamically fetching Sci-Hub mirrors from {url}", file=sys.stderr)
  try:
    html = fetch_generic_url_text(url, timeout=15)
    # Extract all urls from href attributes
    found_urls = re.findall(r'href=["\'](https?://[^"\']+)["\']', html, re.IGNORECASE)
    for u in found_urls:
      parsed = urllib.parse.urlparse(u)
      netloc = parsed.netloc.lower()
      # Match netloc containing sci-hub, sci.hub, or hubg.org, but exclude now.sh
      if ("sci-hub" in netloc or "sci.hub" in netloc or "hubg.org" in netloc) and "now.sh" not in netloc:
        base_url = f"{parsed.scheme}://{parsed.netloc}"
        if base_url not in mirrors:
          mirrors.append(base_url)
    print(f"Diagnostics: Found {len(mirrors)} dynamic mirrors: {mirrors}", file=sys.stderr)
  except Exception as e:
    print(f"Diagnostics: Failed to fetch dynamic mirrors: {e}", file=sys.stderr)
  return mirrors


def is_captcha_or_blocked(html: str) -> bool:
  """Detects if the mirror response page is blocked by a Captcha or DDoS protection."""
  html_lower = html.lower()
  # Common anti-bot or captcha indicators
  block_keywords = [
      "captcha", "altcha", "please verify", "security check", 
      "robot", "hcaptcha", "recaptcha", "cloudflare", "shield", 
      "enter code", "human verification"
  ]
  for keyword in block_keywords:
    if keyword in html_lower:
      return True
  return False


def solve_altcha(challenge_data: dict) -> str:
  """Solves the Altcha Proof-of-Work challenge in Python."""
  import hashlib
  import base64
  import json

  algorithm = challenge_data.get("algorithm", "SHA-256")
  challenge = challenge_data.get("challenge")
  salt = challenge_data.get("salt")
  signature = challenge_data.get("signature")
  max_number = challenge_data.get("maxnumber") or challenge_data.get("maxNumber") or 1000000
  
  if algorithm != "SHA-256":
    raise ValueError(f"Unsupported algorithm: {algorithm}")
    
  print(f"Diagnostics: Solving ALTCHA challenge with max_number={max_number}...", file=sys.stderr)
  
  solved_number = None
  for num in range(max_number + 1):
    test_str = f"{salt}{num}".encode("utf-8")
    h = hashlib.sha256(test_str).hexdigest()
    if h == challenge:
      solved_number = num
      break
      
  if solved_number is None:
    raise ValueError("Could not solve ALTCHA challenge!")
    
  print(f"Diagnostics: Solved ALTCHA! Number={solved_number}", file=sys.stderr)
  
  payload_dict = {
      "algorithm": algorithm,
      "challenge": challenge,
      "number": solved_number,
      "salt": salt,
      "signature": signature
  }
  
  payload_json = json.dumps(payload_dict)
  return base64.b64encode(payload_json.encode("utf-8")).decode("utf-8")


def extract_pdf_url_from_scihub_html(html: str, base_mirror: str) -> Optional[str]:
  """Parses Sci-Hub HTML to find PDF source using tiered matching."""
  def normalize_url(u: str) -> str:
    if u.startswith("//"):
      return "https:" + u
    elif u.startswith("/"):
      return base_mirror.rstrip("/") + u
    return u

  # Tier 1: Look for citation_pdf_url meta tag (standard academic metadata)
  match_meta = re.search(r'name=["\']citation_pdf_url["\']\s+content=["\']([^"\']+)["\']', html, re.IGNORECASE)
  if not match_meta:
    match_meta = re.search(r'content=["\']([^"\']+)["\']\s+name=["\']citation_pdf_url["\']', html, re.IGNORECASE)
  if match_meta:
    return normalize_url(match_meta.group(1))

  # Tier 2: Look for object data attribute (often used for embedded PDF viewer)
  match_obj = re.search(r'<object[^>]+?data=["\']([^"\']+)["\']', html, re.IGNORECASE)
  if match_obj:
    return normalize_url(match_obj.group(1))

  # Tier 3: Look for id="pdf" specifically (most reliable for iframe/embed)
  match_id = re.search(r'<(?:iframe|embed)[^>]+?id=["\']pdf["\'][^>]+?src=["\']([^"\']+)["\']', html, re.IGNORECASE)
  if not match_id:
    match_id = re.search(r'<(?:iframe|embed)[^>]+?src=["\']([^"\']+)["\'][^>]+?id=["\']pdf["\']', html, re.IGNORECASE)
  if match_id:
    return normalize_url(match_id.group(1))

  # Tier 4: Look for iframe/embed with src pointing to typical pdf/downloads paths
  matches = re.findall(r'<(?:iframe|embed)[^>]+?src=["\']([^"\']+)["\']', html, re.IGNORECASE)
  for url in matches:
    url_lower = url.lower()
    if ".pdf" in url_lower or "/downloads/" in url_lower or "/pdf/" in url_lower or "/article/" in url_lower:
      return normalize_url(url)

  # Tier 5: Fallback to standard anchor download links containing pdf paths
  hrefs = re.findall(r'href=["\']([^"\']+)["\']', html, re.IGNORECASE)
  for href in hrefs:
    href_lower = href.lower()
    if ".pdf" in href_lower or "/downloads/" in href_lower or "/pdf/" in href_lower:
      return normalize_url(href)
      
  return None


def try_download_scihub(doi: str) -> Optional[bytes]:
  """Attempts to download paper PDF by crawling dynamic and static Sci-Hub mirrors sequentially."""
  import http.cookiejar
  import ssl
  
  # Get dynamic mirrors first
  mirrors = fetch_dynamic_scihub_mirrors()
  
  # Merge with hardcoded ones
  for m in SCI_HUB_MIRRORS:
    if m not in mirrors:
      mirrors.append(m)
      
  print(f"Diagnostics: Total Sci-Hub mirrors to try: {mirrors}", file=sys.stderr)
  
  for mirror in mirrors:
    print(f"Diagnostics: Trying Sci-Hub mirror: {mirror}", file=sys.stderr)
    url = f"{mirror.rstrip('/')}/{doi}"
    
    # Create a fresh CookieJar and opener for this mirror session to avoid cookie leakage
    cookie_jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(cookie_jar)
    )
    
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    }
    
    try:
      # Step 1: Get mirror landing page HTML (using default SSL context to pass DDoS-Guard)
      req = urllib.request.Request(url, headers=headers)
      try:
        with opener.open(req, timeout=15) as resp:
          final_url = resp.geturl()
          html = resp.read().decode('utf-8', errors='replace')
      except (urllib.error.URLError, ssl.SSLError, OSError) as ssl_err:
        # Fallback to disabled SSL verification context if verification failed
        print(f"Diagnostics: SSL verification error on {mirror}: {ssl_err}. Retrying with SSL verification disabled.", file=sys.stderr)
        ctx_bypass = ssl.create_default_context()
        ctx_bypass.check_hostname = False
        ctx_bypass.verify_mode = ssl.CERT_NONE
        opener_bypass = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(cookie_jar),
            urllib.request.HTTPSHandler(context=ctx_bypass)
        )
        opener = opener_bypass
        with opener.open(req, timeout=15) as resp:
          final_url = resp.geturl()
          html = resp.read().decode('utf-8', errors='replace')

      parsed_url = urllib.parse.urlparse(final_url)
      base_origin = f"{parsed_url.scheme}://{parsed_url.netloc}"
      
      # Step 2: Handle Captcha/blocking
      if is_captcha_or_blocked(html):
        # Check if it is a solvable Altcha captcha
        if "<altcha-widget" in html and "challengeurl" in html:
          print(f"Diagnostics: Solvable Altcha Captcha detected on {final_url}.", file=sys.stderr)
          match = re.search(r'challengeurl\s*=\s*["\']([^"\']+)["\']', html)
          if match:
            challenge_path = match.group(1)
            challenge_url = base_origin.rstrip('/') + challenge_path
            print(f"Diagnostics: Fetching Altcha challenge from {challenge_url}", file=sys.stderr)
            
            # Fetch challenge parameters
            req_chal = urllib.request.Request(challenge_url, headers=headers)
            with opener.open(req_chal, timeout=10) as resp_chal:
              chal_json = json.loads(resp_chal.read().decode('utf-8'))
              
            # Solve it
            solution_b64 = solve_altcha(chal_json)
            
            # POST the solution
            solution_path = challenge_path.replace("challenge", "solution")
            solution_url = base_origin.rstrip('/') + solution_path
            print(f"Diagnostics: Posting Altcha solution to {solution_url}", file=sys.stderr)
            
            post_data = json.dumps({"captcha": solution_b64}).encode('utf-8')
            req_post = urllib.request.Request(
                solution_url,
                data=post_data,
                headers={**headers, 'Content-Type': 'application/json'}
            )
            with opener.open(req_post, timeout=10) as resp_sol:
              sol_res = json.loads(resp_sol.read().decode('utf-8'))
              
            if sol_res.get("success"):
              print(f"Diagnostics: Altcha Captcha solved successfully. Fetching paper page again...", file=sys.stderr)
              # Fetch paper page again (cookies are preserved in opener)
              req_paper = urllib.request.Request(final_url, headers=headers)
              with opener.open(req_paper, timeout=15) as resp_paper:
                html = resp_paper.read().decode('utf-8', errors='replace')
            else:
              print(f"Diagnostics: Failed to solve Altcha Captcha on {mirror}.", file=sys.stderr)
              continue
          else:
            print(f"Diagnostics: Could not extract challengeurl on {mirror}.", file=sys.stderr)
            continue
        else:
          # Unsolvable captcha/blocking page
          print(f"Diagnostics: Unsolvable block or Captcha on mirror {mirror}. Skipping.", file=sys.stderr)
          continue
          
      # Step 3: Extract PDF URL
      pdf_url = extract_pdf_url_from_scihub_html(html, base_origin)
      if pdf_url:
        print(f"Diagnostics: Found PDF URL on Sci-Hub: {pdf_url}", file=sys.stderr)
        
        # If the PDF URL points to a different domain, replace the domain with base_origin
        # to reuse the cookies of the verified session.
        parsed_pdf = urllib.parse.urlparse(pdf_url)
        if parsed_pdf.netloc.lower() != parsed_url.netloc.lower():
          target_pdf_url = base_origin.rstrip('/') + parsed_pdf.path
          if parsed_pdf.query:
            target_pdf_url += "?" + parsed_pdf.query
          print(f"Diagnostics: Rerouting PDF request to verified domain: {target_pdf_url}", file=sys.stderr)
        else:
          target_pdf_url = pdf_url
          
        # Download PDF using the same session
        content = None
        try:
          req_pdf = urllib.request.Request(
              target_pdf_url,
              headers={
                  'User-Agent': headers['User-Agent'],
                  'Referer': final_url
              }
          )
          with opener.open(req_pdf, timeout=40) as resp_pdf:
            content = resp_pdf.read()
        except Exception as e:
          print(f"Diagnostics: Failed to download from rerouted URL {target_pdf_url}: {e}", file=sys.stderr)
          if target_pdf_url != pdf_url:
            try:
              print(f"Diagnostics: Retrying with original PDF URL: {pdf_url}", file=sys.stderr)
              req_orig = urllib.request.Request(
                  pdf_url,
                  headers={
                      'User-Agent': headers['User-Agent'],
                      'Referer': final_url
                  }
              )
              with opener.open(req_orig, timeout=40) as resp_orig:
                content = resp_orig.read()
            except Exception as e_orig:
              print(f"Diagnostics: Failed to download from original URL {pdf_url}: {e_orig}", file=sys.stderr)
              
        if content and validate_pdf_content(content):
          return content
        else:
          print(f"Diagnostics: Downloaded content is not a valid PDF or too small.", file=sys.stderr)
      else:
        print(f"Diagnostics: No PDF URL found on mirror landing page {final_url}", file=sys.stderr)
    except Exception as e:
      print(f"Diagnostics: Sci-Hub mirror {mirror} failed: {e}", file=sys.stderr)
    
    # Short safety backoff between mirrors
    time.sleep(1.5)
  return None




def sync_database(resolved_db_dir: str):
  """Cleans up the database by removing files/directories that do not belong to index.md."""
  index_path = os.path.join(resolved_db_dir, "index.md")
  if not os.path.exists(index_path):
    print(json.dumps({"status": "error", "message": f"index.md not found in {resolved_db_dir}"}, indent=2))
    sys.exit(1)
    
  valid_bases = []
  try:
    with open(index_path, "r", encoding="utf-8") as f:
      for line in f:
        if "|" in line:
          parts = [p.strip() for p in line.split("|")]
          if len(parts) >= 5:
            y, auth, t = parts[1], parts[2], parts[3]
            if y == "年份 (Year)" or y == "---" or "年份" in y:
              continue
            file_base = sanitize_filename(f"{y}-{auth}-{t}")
            valid_bases.append(file_base)
  except Exception as e:
    print(json.dumps({"status": "error", "message": f"Failed to read index.md: {e}"}, indent=2))
    sys.exit(1)
    
  if not valid_bases:
    print(json.dumps({"status": "warning", "message": "No valid papers found in index.md. Cleanup aborted to prevent deleting everything."}, indent=2))
    return
    
  def normalize_str(s: str) -> str:
    return re.sub(r'[^a-zA-Z0-9]', '', s.lower())
    
  def get_lcp_length(s1: str, s2: str) -> int:
    i = 0
    while i < len(s1) and i < len(s2) and s1[i] == s2[i]:
      i += 1
    return i

  def is_relevant(name: str) -> bool:
    name_clean = name.lower()
    if name_clean.endswith(".pdf") or name_clean.endswith(".md"):
      name_clean = name_clean[:-4]
    norm_name = normalize_str(name_clean)
    
    for base in valid_bases:
      norm_base = normalize_str(base)
      lcp_len = get_lcp_length(norm_name, norm_base)
      required_len = min(30, len(norm_name), len(norm_base))
      if lcp_len >= required_len and required_len >= 10:
        return True
    return False

  unprocessed_dir = os.path.join(resolved_db_dir, "pdf", "unprocessed")
  processed_dir = os.path.join(resolved_db_dir, "pdf", "processed")
  md_dir = os.path.join(resolved_db_dir, "md")
  
  removed_count = 0
  
  if os.path.exists(unprocessed_dir):
    for item in os.listdir(unprocessed_dir):
      if item.endswith(".pdf") and not is_relevant(item):
        try:
          os.remove(os.path.join(unprocessed_dir, item))
          removed_count += 1
          print(f"Diagnostics: Removed irrelevant PDF from unprocessed: {item}", file=sys.stderr)
        except Exception as e:
          print(f"Diagnostics: Failed to remove {item}: {e}", file=sys.stderr)
        
  if os.path.exists(processed_dir):
    for item in os.listdir(processed_dir):
      if item.endswith(".pdf") and not is_relevant(item):
        try:
          os.remove(os.path.join(processed_dir, item))
          removed_count += 1
          print(f"Diagnostics: Removed irrelevant PDF from processed: {item}", file=sys.stderr)
        except Exception as e:
          print(f"Diagnostics: Failed to remove {item}: {e}", file=sys.stderr)
        
  if os.path.exists(md_dir):
    for item in os.listdir(md_dir):
      if not is_relevant(item):
        path = os.path.join(md_dir, item)
        try:
          if os.path.isdir(path):
            shutil.rmtree(path)
            print(f"Diagnostics: Removed irrelevant MD dir: {item}", file=sys.stderr)
          else:
            os.remove(path)
            print(f"Diagnostics: Removed irrelevant MD file: {item}", file=sys.stderr)
          removed_count += 1
        except Exception as e:
          print(f"Diagnostics: Failed to remove {item}: {e}", file=sys.stderr)
        
  print(json.dumps({
      "status": "success",
      "message": f"Database synchronization completed. Removed {removed_count} irrelevant files/directories."
  }, indent=2))


def main():
  parser = argparse.ArgumentParser(description="Download and index scientific PDF with metadata naming and Sci-Hub fallback.")
  parser.add_argument("--doi", type=str, help="DOI of the paper (e.g., 10.1038/nature12345)")
  parser.add_argument("--id", type=str, help="arXiv ID of the paper (e.g., 2305.10601)")
  parser.add_argument("--output_dir", type=str, default=DEFAULT_DB_DIR, help="Destination database directory")
  parser.add_argument("--filename", type=str, help="Force override output filename (without extension)")
  parser.add_argument("--mark_processed", action="store_true", help="Mark paper as processed (moves PDF to processed and updates index.md)")
  parser.add_argument("--summary", type=str, default="-", help="A 3-sentence summary of the paper for index.md")
  parser.add_argument("--sync_db", action="store_true", help="Sync database: removes files and directories that do not belong to papers registered in index.md using robust prefix matching.")

  args = parser.parse_args()

  if not args.doi and not args.id and not args.filename and not args.sync_db:
    print(json.dumps({"status": "error", "message": "Must provide either --doi, --id, --filename, or --sync_db"}, indent=2))
    sys.exit(1)

  # 1. Resolve database root directory
  db_dir = args.output_dir
  if db_dir.lower().endswith("pdf") or db_dir.lower().endswith("pdf\\") or db_dir.lower().endswith("pdf/"):
    db_dir = os.path.dirname(db_dir.rstrip(r"\/"))
    
  resolved_db_dir = get_target_db_dir(db_dir)

  if args.sync_db:
    sync_database(resolved_db_dir)
    sys.exit(0)
  
  unprocessed_dir = os.path.join(resolved_db_dir, "pdf", "unprocessed")
  processed_dir = os.path.join(resolved_db_dir, "pdf", "processed")
  index_path = os.path.join(resolved_db_dir, "index.md")

  # Ensure the subdirectories exist
  os.makedirs(unprocessed_dir, exist_ok=True)
  os.makedirs(processed_dir, exist_ok=True)

  # 2. Resolve metadata (Year, Author, Title, Identifier)
  year, author, title = "Unknown", "Unknown", "Paper"
  identifier = "Unknown"
  resolved = False

  if args.id:
    identifier = f"arXiv:{args.id}"
    meta = fetch_arxiv_metadata(args.id)
    if meta:
      year, author, title = meta
      resolved = True
  elif args.doi:
    identifier = f"doi:{args.doi}"
    meta = fetch_crossref_metadata(args.doi)
    if meta:
      year, author, title = meta
      resolved = True
    else:
      meta = fetch_openalex_metadata(args.doi)
      if meta:
        year, author, title = meta
        resolved = True

  # 3. Determine target filename base
  if args.filename:
    file_base = args.filename
    if file_base.lower().endswith(".pdf"):
      file_base = file_base[:-4]
    file_base = sanitize_filename(file_base)
  elif resolved:
    raw_name = f"{year}-{author}-{title}"
    file_base = sanitize_filename(raw_name)
  else:
    raw_name = args.id if args.id else args.doi
    file_base = sanitize_filename(raw_name)

  filename = f"{file_base}.pdf"

  # 4. Handle mark_processed flow
  if args.mark_processed:
    if identifier == "Unknown":
      found_id = find_identifier_in_index(index_path, file_base)
      if found_id:
        identifier = found_id
      else:
        identifier = file_base
        
    if not resolved:
      parts = file_base.split("-", 2)
      if len(parts) == 3:
        year, author, title = parts
      else:
        year, author, title = "Unknown", "Unknown", file_base

    unprocessed_path = os.path.join(unprocessed_dir, filename)
    processed_path = os.path.join(processed_dir, filename)

    if os.path.exists(unprocessed_path):
      try:
        shutil.move(unprocessed_path, processed_path)
        print(f"Diagnostics: Moved {filename} from unprocessed to processed.", file=sys.stderr)
      except Exception as e:
        print(json.dumps({"status": "error", "message": f"Failed to move PDF: {e}"}, indent=2))
        sys.exit(1)
    elif os.path.exists(processed_path):
      print(f"Diagnostics: {filename} already in processed folder.", file=sys.stderr)
    else:
      print(json.dumps({"status": "error", "message": f"PDF file not found in either processed or unprocessed directories: {filename}"}, indent=2))
      sys.exit(3)

    update_index_file(
        index_path=index_path,
        year=year,
        author=author,
        title=title,
        identifier=identifier,
        status="已处理 (Processed)",
        file_base=file_base,
        summary=args.summary
    )

    print(json.dumps({
        "status": "success",
        "message": "Successfully marked paper as processed and updated index.",
        "filepath": processed_path,
        "filename": filename
    }, indent=2))
    sys.exit(0)

  # 5. Handle download flow
  unprocessed_path = os.path.join(unprocessed_dir, filename)
  processed_path = os.path.join(processed_dir, filename)

  if os.path.exists(processed_path) and os.path.getsize(processed_path) >= MIN_PDF_SIZE_BYTES:
    print(json.dumps({
        "status": "skipped",
        "message": "File already exists in processed directory.",
        "filepath": processed_path,
        "filename": filename
    }, indent=2))
    sys.exit(0)

  if os.path.exists(unprocessed_path) and os.path.getsize(unprocessed_path) >= MIN_PDF_SIZE_BYTES:
    print(json.dumps({
        "status": "skipped",
        "message": "File already exists in unprocessed directory.",
        "filepath": unprocessed_path,
        "filename": filename
    }, indent=2))
    sys.exit(0)

  content = None

  if args.id:
    arxiv_url = f"pdf/{args.id}.pdf"
    print(f"Diagnostics: Downloading from arXiv: https://arxiv.org/pdf/{args.id}.pdf", file=sys.stderr)
    try:
      content = _ARXIV_CLIENT.fetch_bytes(arxiv_url, timeout=30)
      if not validate_pdf_content(content):
        content = None
    except Exception as e:
      print(f"Diagnostics: arXiv download failed: {e}", file=sys.stderr)

  elif args.doi:
    content = try_download_direct_oa(args.doi)
    if not content:
      content = try_download_scihub(args.doi)

  if content:
    try:
      with open(unprocessed_path, "wb") as f:
        f.write(content)
      
      update_index_file(
          index_path=index_path,
          year=year,
          author=author,
          title=title,
          identifier=identifier,
          status="未处理 (Unprocessed)",
          file_base=file_base,
          summary="-"
      )

      print(json.dumps({
          "status": "success",
          "message": "Successfully downloaded paper PDF to unprocessed folder.",
          "filepath": unprocessed_path,
          "filename": filename
      }, indent=2))
      sys.exit(0)
    except Exception as e:
      print(json.dumps({
          "status": "error",
          "message": f"Failed to write downloaded content to file: {e}"
      }, indent=2))
      sys.exit(1)
  else:
    print(json.dumps({
        "status": "failed",
        "message": "Could not download PDF from any available source (direct OA and Sci-Hub mirrors all failed)."
    }, indent=2))
    sys.exit(2)


if __name__ == "__main__":
  main()
