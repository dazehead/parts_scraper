import os, re
from collections import defaultdict
import numpy as np
from skimage.io import imread
from skimage.transform import resize
from skimage import img_as_float
from skimage.color import rgb2gray
from PIL import Image
import hmac, hashlib, base64
import io
from dotenv import load_dotenv

from obs import get_logger
from obs.metrics import build_emitter
from tenancy import TenantPaths
from tenancy.ids import validate_tenant_id

load_dotenv()

_log = get_logger("image_proc.processing")
_metrics = build_emitter(stage="image_proc")


class Img_Proc:
    def __init__(self, db, tenant_id=None, testing=False):
        self.testing = testing
        self.folder = "images"
        self.db = db
        # tenant_id may be None for tests / dry runs, but every write
        # path validates it before touching S3 or SQL.
        self.tenant_id = validate_tenant_id(tenant_id) if tenant_id else None
        self.paths = TenantPaths(self.tenant_id) if self.tenant_id else None

        self.bucket = os.getenv("BUCKET")
        self.prefix = os.getenv('IMAGE_KEY')
        self.html_secret = os.getenv('HTML_SECRET')
        if not self.bucket:
            raise RuntimeError("BUCKET environment variable is required")
        if not self.html_secret:
            raise RuntimeError("HTML_SECRET environment variable is required")
        self.region = os.getenv("AWS_REGION", "us-east-1")
        self.final_url = f"https://{self.bucket}.s3.{self.region}.amazonaws.com/"


    def group_images(self):
        self.images = [f for f in os.listdir(self.folder) if os.path.isfile(os.path.join(self.folder, f))]
        self.grouped = defaultdict(list)
        for name in self.images:
            base_name = '_'.join(name.split('_')[:-1])  # strip the numeric suffix
            self.grouped[base_name].append(name)

    # ---------- Image IO / preprocessing ----------

    def load_and_resize(self, path, where="(unknown)", size=(600, 600)):
        """Load image from path and resize to target size (default 600x600)."""

        img = imread(path)
        img = img_as_float(img)  # scale to [0,1]
        img = np.squeeze(img)

        if img.ndim != 2 and img.ndim != 3:
            raise ValueError(f"{where}: expected 2-D or 3-D, got {img.shape}")

        # resize to fixed dimensions
        img = resize(img, size, anti_aliasing=True)
        return img

    def to_grayscale(self,img, where="(unknown)"):
        """Convert an image to grayscale float32 2D array."""
        if img.ndim == 3:
            if img.shape[2] == 4:  # drop alpha if present
                img = img[:, :, :3]
            img = rgb2gray(img)  # -> HxW float

        if img.ndim != 2:
            raise ValueError(f"{where}: expected 2-D, got {img.shape}")

        if img.dtype == object:
            img = np.array(img, dtype=np.float32)
        else:
            img = np.ascontiguousarray(img, dtype=np.float32)

        return img


    def resize_image(self, img, shape=(16, 16)):
        out = resize(img, shape, anti_aliasing=True)
        return np.ascontiguousarray(out, dtype=np.float32)

    # ---------- Orientation helpers (top-left bright mass) ----------
    def _make_tl_weights(self, h: int, w: int, kind: str = "gaussian"):
        yy, xx = np.mgrid[0:h, 0:w]
        if kind == "gaussian":
            sy, sx = h / 3.0, w / 3.0
            wmap = np.exp(- (yy**2)/(2*sy*sy) - (xx**2)/(2*sx*sx))
        else:
            wy = 1.0 - (yy / max(h-1, 1))
            wx = 1.0 - (xx / max(w-1, 1))
            wmap = wy * wx
        wmap /= (wmap.sum() + 1e-12)
        return wmap.astype(np.float32)

    def _score_top_left(self, img2d: np.ndarray, wmap: np.ndarray) -> float:
        return float((img2d * wmap).sum())

    def _dihedral_variants(self, img2d: np.ndarray):
        variants = []
        for k in range(4):  # 0,90,180,270
            r = np.rot90(img2d, k=k)
            variants.append((r, f"rot{k*90}"))
            variants.append((np.fliplr(r), f"rot{k*90}_fliplr"))
        return variants

    def orient_top_left(self, img2d: np.ndarray, weights_kind: str = "gaussian"):
        assert img2d.ndim == 2, f"Expected 2D image, got {img2d.shape}"
        h, w = img2d.shape
        wmap = self._make_tl_weights(h, w, kind=weights_kind)

        best_img, best_desc, best_score = None, None, -np.inf
        for v, desc in self._dihedral_variants(img2d):
            s = self._score_top_left(v, wmap)
            if s > best_score:
                best_img, best_desc, best_score = v, desc, s
        return np.ascontiguousarray(best_img, dtype=np.float32), best_desc, best_score



# --- Perceptual hashing utilities ---

    def _bits_to_int(self, bits: np.ndarray) -> int:
        """Pack a boolean/0-1 array into a single integer (row-major)."""
        # Flatten, convert to string of 0/1, then to int base-2
        return int(''.join('1' if b else '0' for b in bits.astype(bool).ravel()), 2)

    def _hamming(self, h1: int, h2: int) -> int:
        """Hamming distance between two packed integer hashes."""
        return (h1 ^ h2).bit_count()

    def compute_hash(self, img16: np.ndarray, method: str = "phash", hash_size: int = 8) -> int:
        """
        Compute a perceptual hash for a small grayscale image in [0,1].
        Supported methods:
        - 'phash' (DCT-based; preferred if SciPy available)
        - 'ahash' (average hash)
        - 'dhash' (difference hash, horizontal)
        Returns an integer with hash_size*hash_size (or hash_size*(hash_size-1) for dhash) bits.
        """
        assert img16.ndim == 2, f"Expected 2D image, got {img16.shape}"
        img = np.clip(img16, 0.0, 1.0).astype(np.float32)

        if method == "phash":
            # Prefer pHash (DCT of a slightly larger image, keep low freq)
            try:
                from scipy.fftpack import dct
            except Exception:
                # Fallback to aHash if SciPy not installed
                method = "ahash"

        if method == "phash":
            # 1) Upscale to 32x32 to capture more low-frequency detail before DCT
            big = self.resize_image(img, shape=(32, 32))

            # 2) 2D DCT (type-II) row-wise then col-wise
            from scipy.fftpack import dct
            dct_rows = dct(big, type=2, norm='ortho', axis=0)
            dct2 = dct(dct_rows, type=2, norm='ortho', axis=1)

            # 3) Take top-left low-frequency block (hash_size x hash_size)
            low = dct2[:hash_size, :hash_size]

            # 4) Zero out the DC term before computing the median (classic pHash tweak)
            low_no_dc = low.copy()
            low_no_dc[0, 0] = 0.0

            median = np.median(low_no_dc)
            bits = low > median  # boolean matrix
            return self._bits_to_int(bits)

        elif method == "ahash":
            # Average hash: resize to hash_size x hash_size, threshold at mean
            small = self.resize_image(img, shape=(hash_size, hash_size))
            mean = small.mean()
            bits = small >= mean
            return self._bits_to_int(bits)

        elif method == "dhash":
            # Difference hash (horizontal): (hash_size x (hash_size+1)) -> compare adjacent cols
            w = hash_size + 1
            small = self.resize_image(img, shape=(hash_size, w))
            diff = small[:, 1:] > small[:, :-1]  # shape (hash_size, hash_size)
            return self._bits_to_int(diff)

        else:
            raise ValueError(f"Unknown hash method: {method}")

    def hash_and_compare_group(self, files, method="phash", hash_size=8, distance_thresh=10, testing=False):
        """
        For a list of filenames, compute oriented 16x16, hash them,
        and print similar pairs (Hamming distance <= threshold).
        """
        entries = []  # (name, hash_int)
        tracker = 0
        for i, fn in enumerate(files):
            #path = fn
            path = f'images/image_{i}.png'


            try:
                #gray = self.load_and_grayscale(path, where=fn)
                img = self.load_and_resize(path)
                gray = self.to_grayscale(img)
                small = self.resize_image(gray, shape=(16, 16))
                oriented, desc, _ = self.orient_top_left(small)

                h = self.compute_hash(oriented, method=method, hash_size=hash_size)
                entries.append((path, h))


            except Exception as e:
                print(f"  [skip] {path}: {e}")

        # Compare all pairs
        final_set = set()
        n = len(entries)
        for i in range(n):
            for j in range(i + 1, n):
                name1, h1 = entries[i]
                name2, h2 = entries[j]
                d = self._hamming(h1, h2)
                if d <= distance_thresh:
                    #print(f"  similar: {name1} ↔ {name2} | {method} dist={d}")
                    final_set.update((name1, name2))

        return sorted(final_set)

    def run_hashing(self, method="phash", hash_size=8, distance_thresh=10):
        """
        Iterate your grouped files, hash within each group, and print similar pairs.
        """
        for group_name, files in self.grouped.items():
            if len(files) < 2:
                continue
            print(f"\n=== Hashing group: {group_name} ({method}) ===")
            self.hash_and_compare_group(files, method=method, hash_size=hash_size,
                                        distance_thresh=distance_thresh, testing=self.testing)


    def retrieve_from_s3_and_run(self, grouped):
        """download from s3 puts them in images folder then processes them for similarities"""
        grouped_strings = ['/'.join(x.split('/')[-2:]) for x in grouped]

        display_string = grouped_strings[0].split('/')[1:][0]
        display_string = ' '.join(display_string.split('_')[:-1])

        self.group_map = self.db.download_group(self.bucket, grouped_strings)

        keep = self.try_mulitiple_hashes(grouped_strings)
        keep = [keep[0]]  # only grabs the first one to keep

        if len(keep[0]) == 18:
            keep = [self.group_map[keep[0]]]

        self.db.save_data_for_deletion_img_proc(grouped_strings, keep)

        # Metric: dedup discarded (group_size - 1) candidates per part.
        discarded = max(0, len(grouped_strings) - 1)
        _metrics.count("ImagesDiscardedByDedup", discarded)
        _metrics.count("ImagesKept", 1)
        _log.info(
            "group reduced",
            group=display_string,
            candidates=len(grouped_strings),
            discarded=discarded,
            kept=1,
        )

        img = self.grab_image_and_implement_watermark(keep, False)
        hash_key = self.hash_key(keep, self.html_secret)

        pil = self.to_pil(img)
        buf = io.BytesIO()
        pil.save(buf, format='PNG', optimize=True)
        buf.seek(0)

        # Tenant-scope the destination key. hash_key already starts with
        # "final/"; when running in a tenant context we promote that
        # under tenants/<tenant_id>/.
        if self.paths is not None:
            hash_key = self.paths.normalise(hash_key)

        self.db.s3.put_object(
            Bucket=self.bucket,
            Key=hash_key,
            Body=buf.getvalue(),
            ContentType='image/png'
        )

        s = keep[0]
        m = re.search(r'(?<=/)[^_]+(?=_)', s)
        number = m.group(0)
        if self.tenant_id is not None:
            self.db.execute_sql(
                """
                UPDATE dbo.parts
                SET final_tag = :final_tag
                WHERE tenant_id = :tenant_id AND [number] = :number;
                """,
                params={
                    "final_tag": f"{self.final_url}{hash_key}",
                    "tenant_id": self.tenant_id,
                    "number": number,
                },
            )
        else:
            # Legacy / pre-cutover code paths only — should not run in production.
            self.db.execute_sql(
                """
                UPDATE dbo.parts
                SET final_tag = :final_tag
                WHERE [number] = :number;
                """,
                params={
                    "final_tag": f"{self.final_url}{hash_key}",
                    "number": number,
                },
            )
        _log.info(
            "final tag written",
            tenant_id=self.tenant_id,
            part_number=number,
            key=hash_key,
        )

        self.db.empty_dir('images')


    def try_mulitiple_hashes(self, grouped_strings):
        configs = [
            {"method": "phash", "hash_size": 8, "thresholds": [10, 14, 20]},
            {"method": "ahash", "hash_size": 16, "thresholds": [12, 18]},
            {"method": "dhash", "hash_size": 8, "thresholds": [8, 12]},
        ]

        for cfg in configs:
            for t in cfg["thresholds"]:
                keep = self.hash_and_compare_group(
                    grouped_strings,
                    method=cfg["method"],
                    hash_size=cfg["hash_size"],
                    distance_thresh=t,
                    testing=False,
                )
                if keep:
                    return keep                
        return [grouped_strings[0]]
    
    def grab_image_and_implement_watermark(self, keep, to_watermark=False):

        keep_value = keep[0]

        keep_path = next((k for k, v in self.group_map.items() if v == keep_value), None)
        img = imread(keep_path)
        watermark = imread('image_proc/watermark.png')
        if to_watermark:
            out = self.add_watermark_center(img, watermark, scale=0.99, opacity=.60)
            return out
        else:
            return img


    def hash_key(self, keep, secret):
        keep_value = keep[0]
        splited = keep_value.split('/')[1:][0]
        msg = splited.split('.')[:-1][0]

        sig = hmac.new(secret.encode("utf-8"),
                    msg.encode("utf-8"),
                    hashlib.sha256).digest()
        # URL/HTML attribute safe (no + / =)
        new_value = base64.urlsafe_b64encode(sig).decode().rstrip("=")
        return f"final/{new_value}.png"

    def add_watermark_center(self, img, wm, scale=0.12, opacity=0.65):
        """
        Center the watermark on the image, scaling it to `scale` * base width.
        `img`, `wm` are ndarrays (RGB/RGBA or grayscale). Returns uint8 RGB.
        """
        base = img.copy()
        if base.ndim == 2:
            base = np.stack([base]*3, axis=-1)
        if base.shape[2] == 4:
            base = base[:, :, :3]
        H, W = base.shape[:2]

        # Split watermark into RGB + alpha
        if wm.ndim == 2:
            overlay = np.stack([wm]*3, axis=-1).astype(np.uint8)
            alpha = np.ones(wm.shape, dtype=np.float32)
        elif wm.shape[2] == 4:
            overlay = wm[:, :, :3].astype(np.uint8)
            alpha = (wm[:, :, 3].astype(np.float32) / 255.0)
        else:
            overlay = wm.astype(np.uint8)
            alpha = np.ones(wm.shape[:2], dtype=np.float32)

        # Resize watermark to a fraction of base width
        target_w = max(1, int(W * float(scale)))
        ratio = target_w / overlay.shape[1]
        new_size = (target_w, max(1, int(overlay.shape[0] * ratio)))
        overlay = np.array(Image.fromarray(overlay).resize(new_size, Image.LANCZOS))
        alpha   = np.array(Image.fromarray((alpha * 255).astype(np.uint8)).resize(new_size, Image.LANCZOS)).astype(np.float32) / 255.0

        # Center coords
        h, w = overlay.shape[:2]
        y0 = (H - h) // 2; x0 = (W - w) // 2
        y1 = y0 + h; x1 = x0 + w

        # Blend
        roi = base[y0:y1, x0:x1].astype(np.float32)
        a = (alpha * float(opacity))[..., None]
        blended = a * overlay.astype(np.float32) + (1.0 - a) * roi
        base[y0:y1, x0:x1] = np.clip(blended, 0, 255).astype(np.uint8)
        return base
    
    def to_pil(self, arr, *, bgr=False):
        a = arr
        if a.dtype != np.uint8:
            a = np.clip(a, 0, 255).astype(np.uint8)
        if a.ndim == 2:
            return Image.fromarray(a, mode="L")
        if a.shape[2] == 3:
            if bgr:  # OpenCV -> convert BGR to RGB
                a = a[:, :, ::-1]
            return Image.fromarray(a, mode="RGB")
        if a.shape[2] == 4:
            if bgr:  # BGRA -> RGBA
                a = a[:, :, [2,1,0,3]]
            return Image.fromarray(a, mode="RGBA")
        raise ValueError("unsupported image shape")

if __name__ == "__main__":
    img_proc = Img_Proc(db=None, testing=False)
    img_proc.retrieve_from_s3_and_run(os.environ["BUCKET"])
