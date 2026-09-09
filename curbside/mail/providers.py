"""Mail provider adapters.

Design rule: a live send is possible but never accidental. It requires an
explicit provider selection, credentials in the environment, a configured
from-address, and per-run confirmation. Absent any of those, the dry-run
provider records intent and sends nothing.

Direct mail is not covered by the FTC/FCC Do Not Call registry — that is
telephony. Mail suppression is a separate voluntary stack:
  - DMAchoice (ANA) consumer opt-out
  - USPS Deceased Do Not Contact
  - NCOALink move update (also required for postal discounts)
Plus our own local list, which is what `suppression.txt` implements.
"""
import base64
import json
import os
import pathlib
import time
import urllib.error
import urllib.request


class MailError(Exception):
    pass


class UndeliverableError(MailError):
    pass


# --------------------------------------------------------------------------
# Suppression
# --------------------------------------------------------------------------

def load_suppression(path):
    p = pathlib.Path(path)
    if not p.exists():
        return set()
    from curbside.store import address_key
    return {address_key(l) for l in p.read_text().splitlines()
            if l.strip() and not l.startswith("#")}


def is_suppressed(address, suppression):
    from curbside.store import address_key
    return address_key(address) in suppression


# --------------------------------------------------------------------------
# Base
# --------------------------------------------------------------------------

class MailProvider:
    name = "base"
    cost_per_piece = 0.0
    live = False

    def verify(self, address):
        raise NotImplementedError

    def send(self, lead, postcard_path, **kw):
        raise NotImplementedError

    def preflight(self):
        """Raise unless this provider is fully configured to send."""
        return True


# --------------------------------------------------------------------------
# Dry run — the default
# --------------------------------------------------------------------------

class DryRunProvider(MailProvider):
    name = "dryrun"
    cost_per_piece = 0.75
    live = False

    def __init__(self, outbox=None, **kw):
        self.outbox = pathlib.Path(outbox or "var/outbox")

    def verify(self, address):
        parts = [p.strip() for p in address.split(",")]
        has_number = bool(parts) and any(c.isdigit() for c in parts[0])
        zip_ok = bool(parts) and any(
            len(t) == 5 and t.isdigit() for t in parts[-1].split())
        ok = len(parts) >= 3 and has_number and zip_ok
        return {
            "deliverable": ok,
            "verified_address": address if ok else None,
            "provider": self.name,
            "note": "structural check only — NOT CASS certified",
        }

    def send(self, lead, postcard_path, **kw):
        self.outbox.mkdir(parents=True, exist_ok=True)
        rec = {
            "lead_id": lead["id"],
            "address": lead["address"],
            "postcard": str(postcard_path),
            "cost_usd": self.cost_per_piece,
            "provider": self.name,
            "sent_at": time.time(),
            "dry_run": True,
        }
        f = self.outbox / f"{lead['id']:06d}.json"
        f.write_text(json.dumps(rec, indent=2))
        return {"id": f"dryrun-{lead['id']}", "cost": self.cost_per_piece,
                "receipt": str(f), "live": False}


# --------------------------------------------------------------------------
# Lob — real CASS verification and real print + mail
# --------------------------------------------------------------------------

class LobProvider(MailProvider):
    """https://docs.lob.com

    Environment:
      LOB_API_KEY          test_* or live_*
      LOB_FROM_NAME        return-address name (legally required on the piece)
      LOB_FROM_LINE1       street
      LOB_FROM_CITY        city
      LOB_FROM_STATE       two-letter
      LOB_FROM_ZIP         5-digit
      CURBSIDE_ALLOW_LIVE_MAIL=1   required for a live_* key to actually send
    """
    name = "lob"
    cost_per_piece = 0.75
    BASE = "https://api.lob.com/v1"

    def __init__(self, api_key=None, **kw):
        self.key = (api_key or os.environ.get("LOB_API_KEY", "")).strip()
        if not self.key:
            raise MailError("LOB_API_KEY not set")
        self.test_mode = self.key.startswith("test_")
        self.live = not self.test_mode
        self.from_address = {
            "name":    os.environ.get("LOB_FROM_NAME", "").strip(),
            "address_line1": os.environ.get("LOB_FROM_LINE1", "").strip(),
            "address_city":  os.environ.get("LOB_FROM_CITY", "").strip(),
            "address_state": os.environ.get("LOB_FROM_STATE", "").strip(),
            "address_zip":   os.environ.get("LOB_FROM_ZIP", "").strip(),
        }

    # -- http ------------------------------------------------------------

    def _request(self, path, payload=None, method="POST", files=None):
        auth = base64.b64encode(f"{self.key}:".encode()).decode()
        headers = {"Authorization": f"Basic {auth}"}
        data = None
        if files:
            boundary = "----curbside" + str(int(time.time() * 1000))
            body = b""
            for k, v in (payload or {}).items():
                body += (f"--{boundary}\r\nContent-Disposition: form-data; "
                         f'name="{k}"\r\n\r\n{v}\r\n').encode()
            for k, (fn, blob, ct) in files.items():
                body += (f"--{boundary}\r\nContent-Disposition: form-data; "
                         f'name="{k}"; filename="{fn}"\r\n'
                         f"Content-Type: {ct}\r\n\r\n").encode() + blob + b"\r\n"
            body += f"--{boundary}--\r\n".encode()
            headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
            data = body
        elif payload is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(payload).encode()

        req = urllib.request.Request(f"{self.BASE}{path}", data=data,
                                     headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            raise MailError(f"lob HTTP {e.code}: {e.read().decode()[:400]}")

    # -- api -------------------------------------------------------------

    def preflight(self):
        missing = [k for k, v in self.from_address.items() if not v]
        if missing:
            raise MailError(
                "Lob from-address incomplete; a physical return address is "
                f"legally required on mailed pieces. Missing: {missing}")
        if self.live and os.environ.get("CURBSIDE_ALLOW_LIVE_MAIL") != "1":
            raise MailError(
                "Refusing to send with a LIVE Lob key. This bills real money "
                "and mails real people. Set CURBSIDE_ALLOW_LIVE_MAIL=1 to "
                "confirm, or use a test_* key.")
        self._request("/us_verifications",
                      {"address": "185 Berry St, San Francisco, CA 94107"})
        return True

    def verify(self, address):
        d = self._request("/us_verifications", {"address": address})
        deliverability = d.get("deliverability")
        line1 = d.get("primary_line", "")
        line2 = d.get("last_line", "")
        return {
            "deliverable": deliverability in ("deliverable",
                                              "deliverable_unnecessary_unit"),
            "verified_address": f"{line1}, {line2}".strip(", ") or None,
            "provider": self.name,
            "raw_deliverability": deliverability,
            "components": d.get("components", {}),
        }

    def send(self, lead, postcard_path, back_path=None, **kw):
        self.preflight()
        v = self.verify(lead["address"])
        if not v["deliverable"]:
            raise UndeliverableError(
                f"{lead['address']} -> {v['raw_deliverability']}")

        comp = v.get("components", {})
        to = {
            "name": kw.get("to_name") or "Current Resident",
            "address_line1": comp.get("primary_number", "") + " " +
                             comp.get("street_name", "") + " " +
                             comp.get("street_suffix", ""),
            "address_city": comp.get("city", ""),
            "address_state": comp.get("state", ""),
            "address_zip": comp.get("zip_code", ""),
        }
        to["address_line1"] = " ".join(to["address_line1"].split())

        front = pathlib.Path(postcard_path).read_bytes()
        payload = {
            "description": f"curbside lead {lead['id']}",
            "size": "6x9",
            "to[name]": to["name"],
            "to[address_line1]": to["address_line1"],
            "to[address_city]": to["address_city"],
            "to[address_state]": to["address_state"],
            "to[address_zip]": to["address_zip"],
            "from[name]": self.from_address["name"],
            "from[address_line1]": self.from_address["address_line1"],
            "from[address_city]": self.from_address["address_city"],
            "from[address_state]": self.from_address["address_state"],
            "from[address_zip]": self.from_address["address_zip"],
        }
        files = {"front": ("front.jpg", front, "image/jpeg")}
        if back_path and pathlib.Path(back_path).exists():
            files["back"] = ("back.jpg",
                             pathlib.Path(back_path).read_bytes(), "image/jpeg")
        else:
            payload["back"] = ("<html><body style='font-family:sans-serif;padding:2rem'>"
                               "<p>Mailed by " + self.from_address["name"] + ".</p>"
                               "<p>To stop receiving these, reply to the address above.</p>"
                               "</body></html>")

        d = self._request("/postcards", payload=payload, files=files)
        return {"id": d.get("id"), "cost": self.cost_per_piece,
                "receipt": d.get("url"), "live": self.live,
                "expected_delivery": d.get("expected_delivery_date")}


PROVIDERS = {"dryrun": DryRunProvider, "lob": LobProvider}


def get_provider(name="dryrun", **kw):
    if name not in PROVIDERS:
        raise MailError(f"unknown provider {name!r}; have {sorted(PROVIDERS)}")
    return PROVIDERS[name](**kw)
