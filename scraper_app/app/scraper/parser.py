from PIL import Image
import boto3
import io, json, os, requests, subprocess, time, sys
from dotenv import load_dotenv
import pandas as pd
from bs4 import BeautifulSoup
from urllib.parse import quote, quote_plus
from urllib3.util.retry import Retry
from requests.adapters import HTTPAdapter
from requests.exceptions import ProxyError, ConnectTimeout, ReadTimeout, SSLError, RequestException

from obs import get_logger
from obs.metrics import build_emitter
from tenancy import TenantPaths
from tenancy.ids import validate_tenant_id

pd.set_option("display.max_colwidth", None)
load_dotenv()

_log = get_logger("scraper.parser")
_metrics = build_emitter(stage="scraper")

class Parser:
    def __init__(self, db, text, tenant_id):
        # tenant_id is validated by the caller, but we re-validate here
        # because Parser is small and gets called per-row.
        self.tenant_id = validate_tenant_id(tenant_id)
        self.paths = TenantPaths(self.tenant_id)

        username = os.getenv("DECODO_USERNAME")
        password = os.getenv("DECODO_PASSWORD")
        username = quote(username, safe='')
        password = quote(password, safe='')

        self.query = text
        self.url = f"https://www.bing.com/images/search?q={self.query}&form=HDRSC2"
        self.proxy_url = f"http://{username}:{password}@gate.decodo.com:10001"
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/126.0.0.0 Safari/537.36"
        }
        self.timeout = 5


        self.db = db
        self.s3 = boto3.client("s3")
        self.bucket = os.getenv("BUCKET")
        self.region = os.getenv("AWS_REGION", "us-east-1")
        if not self.bucket:
            raise RuntimeError("BUCKET environment variable is required")

        self.links = []
        self.tor = None
        self.max_images = 10
        self.images_downloaded = 0

    def tor_start(self):
        exe_path = "/usr/bin/tor"#os.getenv("TOR_PATH")
        self.tor = subprocess.Popen(exe_path,stdout=subprocess.DEVNULL)
        time.sleep(8)
    
    def terminate_tor(self):
        self.tor.terminate()
        time.sleep(2)


    def build_session(self):
        session = requests.Session()
        # Don’t inherit host/container proxy env; we’ll pass proxies explicitly.
        session.trust_env = False
        retry = Retry(
            total=2,
            backoff_factor=0.4,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET"],
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry)
        session.mount("https://", adapter)
        session.mount("http://", adapter)

        return session


    def get_links(self):
        self.session = self.build_session()

        try:
            resp = self._fetch(use_proxy=True)
        except (ProxyError, ConnectTimeout, ReadTimeout, SSLError) as e:
            print(f"[bing] proxy path failed: {e}; retrying direct...")
            resp = self._fetch(use_proxy=False)
        except RequestException:
            # Non-proxy fatal (e.g., 4xx other than 407) — rethrow so caller can DLQ
            raise

        self.links = self._extract_links(resp.text)[:30]
        return self.links

    def _extract_links(self, html):
        """Parse Bing's image-search response and return ordered candidate URLs.

        Bing has historically wrapped each result in <a class="iusc" m='{...}'>.
        We keep that as the primary selector but fall back to any element
        with an ``m`` attribute that JSON-decodes to a dict containing
        ``murl``. As a last resort we regex over the raw HTML. Each fallback
        is logged so a silent layout change is visible to the operator.
        """
        import re

        soup = BeautifulSoup(html, "html.parser")
        links = []
        seen = set()

        def _consume(payload):
            try:
                data = json.loads(payload)
            except (json.JSONDecodeError, TypeError):
                return
            url = data.get("murl") if isinstance(data, dict) else None
            if url and url not in seen:
                seen.add(url)
                links.append(url)

        # Primary: a.iusc[m]
        for a in soup.select("a.iusc[m]"):
            _consume(a.get("m"))

        # Fallback 1: any element with an `m` attribute whose JSON has murl.
        if not links:
            for el in soup.find_all(attrs={"m": True}):
                _consume(el.get("m"))
            if links:
                print("[bing] primary selector empty; matched via attribute fallback")

        # Fallback 2: regex straight over the HTML.
        if not links:
            for match in re.finditer(r'"murl":"(https?:[^"\\]+)"', html):
                url = match.group(1)
                if url not in seen:
                    seen.add(url)
                    links.append(url)
            if links:
                print("[bing] selector fallbacks empty; matched via regex fallback")

        if not links:
            print("[bing] WARNING: no candidate URLs found in response; HTML layout may have changed")

        return links


    def download_images(self, keep_bytes=True):
        """ Downloads from requests resizes to 600x600 and saves them to s3 buckets"""

        self.tor_start()

        session = requests.Session()
        session.proxies = {
            'http':  'socks5h://127.0.0.1:9050',
            'https': 'socks5h://127.0.0.1:9050'
        }

        tag_values = []
        part_id = None

        try:
            info = self.query
            print(info)
            part_number = info.split(" ")[0]
            part_id = self.db.read_sql_query(
                "SELECT part_id FROM dbo.parts "
                "WHERE tenant_id = :tenant_id AND number = :number",
                params={"tenant_id": self.tenant_id, "number": part_number},
            )
            if part_id.empty:
                _log.warning(
                    "no part_id for query; skipping row",
                    tenant_id=self.tenant_id,
                    part_number=part_number,
                )
                return None, []
            part_id = int(part_id["part_id"].iat[0])

            while self.images_downloaded < self.max_images:
                url = self.links.pop()
                file_name = info.replace(" ", "_") + "_" + str(self.images_downloaded) + ".png"
                file_name = file_name.replace('/', "_")
                file_name = file_name.replace(',', '')
                s3_key = self.paths.image_key(file_name)

                session.headers.update({
                    "User-Agent": (
                        "Mozilla/5.0 (Windows Nt 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/114.0.0.0 Safari/537.36"
                    ),
                    "Referer": f'https://www.google.com/search?tbm=isch&q={info.replace(" ","+")}'
                })
                try:
                    with session.get(url, stream=True, timeout=self.timeout) as resp:
                        if resp.status_code == 403:
                            continue
                        resp.raise_for_status()

                        img = Image.open(io.BytesIO(resp.content))
                        img = img.convert("RGBA") if img.mode in ("P", "LA") else img.convert("RGB")
                        img = img.resize((600, 600), Image.LANCZOS)

                        buf = io.BytesIO()
                        img.save(buf, format='PNG')
                        buf.seek(0)

                        self.s3.put_object(
                            Bucket=self.bucket,
                            Key=s3_key,
                            Body=buf.getvalue(),
                            ContentType='image/png'
                        )
                        _log.info(
                            "image uploaded",
                            tenant_id=self.tenant_id,
                            part_number=part_number,
                            s3_key=s3_key,
                            bucket=self.bucket,
                        )
                        _metrics.count("ImagesDownloaded", Tenant=self.tenant_id)
                        self.images_downloaded += 1


                except Exception as e:
                    _log.warning(
                        "image fetch failed",
                        tenant_id=self.tenant_id,
                        part_number=part_number,
                        url=url,
                        error=str(e),
                    )
                    _metrics.count("ImageFetchErrors", Tenant=self.tenant_id)

                else:
                    url_value = f"https://{self.bucket}.s3.{self.region}.amazonaws.com/{s3_key}"
                    tag_values.append(url_value)

        except Exception as e:
            print("Request failed", e)
        
        self.terminate_tor()

        return part_id, tag_values



    def _fetch(self, use_proxy: bool):
        kw = {"headers": self.headers, "timeout": self.timeout}
        if use_proxy and self.proxy_url:
            kw["proxies"] = {"http": self.proxy_url, "https": self.proxy_url}

        r = self.session.get(self.url, **kw)

        # Explicitly treat proxy-auth failures as proxy errors so we can fallback
        if r.status_code == 407 and use_proxy:
            raise ProxyError("HTTP 407 Proxy Authentication Required")

        r.raise_for_status()
        return r
    
if __name__ == "__main__":
    scraper = Parser()
    scraper.run_driver(
        function=scraper.duck_image_search,
        iterations=4)# can do len(self.df)
