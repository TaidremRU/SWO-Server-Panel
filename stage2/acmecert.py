"""Сертификат Let's Encrypt для панели игроков — минимальный ACME v2 (RFC 8555).

Проверка домена — HTTP-01: Let's Encrypt приходит на http://<домен>/.well-known/
acme-challenge/<token>, и панель игроков (порт 80) отвечает ключом из
``Acme.challenges``. Поэтому снаружи должен быть открыт 80-й порт, а домен —
указывать на внешний IP сервера.

Без certbot и без внешних программ: только ``cryptography`` (ключи, CSR, подпись
JWS ES256) и urllib. Файлы — в ``<base_dir>/certs``:
    acme_account.key          — ключ учётной записи ACME (общий для доменов)
    <домен>/privkey.pem       — ключ сертификата
    <домен>/fullchain.pem     — сертификат + цепочка
"""

import base64
import hashlib
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.x509.oid import NameOID

DIRECTORY = "https://acme-v02.api.letsencrypt.org/directory"
DIRECTORY_STAGING = "https://acme-staging-v02.api.letsencrypt.org/directory"
RENEW_DAYS = 30          # продлевать, когда до конца осталось меньше
UA = "SWO-Server-Panel-acme/1.0"


class AcmeError(Exception):
    pass


def _b64(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def _write_atomic(path, data):
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def _load_or_create_key(path):
    if os.path.exists(path):
        with open(path, "rb") as f:
            return serialization.load_pem_private_key(f.read(), password=None)
    key = ec.generate_private_key(ec.SECP256R1())
    _write_atomic(path, key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                          serialization.NoEncryption()))
    return key


def cert_paths(base, domain):
    d = os.path.join(base, "certs", domain)
    return os.path.join(d, "fullchain.pem"), os.path.join(d, "privkey.pem")


def cert_info(base, domain):
    """Что лежит на диске для домена: {exists, not_after, days_left, issuer, names} или {exists: False}."""
    full, key = cert_paths(base, domain)
    if not (domain and os.path.exists(full) and os.path.exists(key)):
        return {"exists": False}
    try:
        with open(full, "rb") as f:
            c = x509.load_pem_x509_certificate(f.read())
        na = c.not_valid_after_utc
        try:
            names = c.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(
                x509.DNSName)
        except x509.ExtensionNotFound:
            names = []
        iss = c.issuer.get_attributes_for_oid(NameOID.ORGANIZATION_NAME) or c.issuer.get_attributes_for_oid(
            NameOID.COMMON_NAME)
        return {"exists": True, "not_after": na.strftime("%Y-%m-%d %H:%M UTC"),
                "days_left": round((na - datetime.now(timezone.utc)).total_seconds() / 86400, 1),
                "issuer": iss[0].value if iss else "", "names": names,
                "staging": "STAGING" in c.issuer.rfc4514_string().upper()}
    except Exception as e:  # noqa: BLE001
        return {"exists": True, "error": "не читается: %s" % e}


class Acme:
    """Выпуск сертификата; ``challenges`` отдаёт веб-сервер на порту 80."""

    def __init__(self, base, email="", staging=False):
        self.base = base
        self.email = (email or "").strip()
        self.dir_url = DIRECTORY_STAGING if staging else DIRECTORY
        self.challenges = {}       # token -> key authorization
        self._lock = threading.Lock()
        os.makedirs(os.path.join(base, "certs"), exist_ok=True)
        acc_name = "acme_account_staging.key" if staging else "acme_account.key"
        self._key = _load_or_create_key(os.path.join(base, "certs", acc_name))
        pub = self._key.public_key().public_numbers()
        self._jwk = {"crv": "P-256", "kty": "EC",
                     "x": _b64(pub.x.to_bytes(32, "big")), "y": _b64(pub.y.to_bytes(32, "big"))}
        self._thumb = _b64(hashlib.sha256(
            json.dumps(self._jwk, sort_keys=True, separators=(",", ":")).encode()).digest())
        self._dir = None
        self._nonce = None
        self._kid = None

    # ------------------------------------------------------------- транспорт
    def _http(self, url, data=None, method=None, headers=None):
        hdr = {"User-Agent": UA}
        hdr.update(headers or {})
        req = urllib.request.Request(url, data=data, method=method, headers=hdr)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, dict(r.headers), r.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read()

    def _directory(self):
        if self._dir is None:
            st, _, body = self._http(self.dir_url)
            if st != 200:
                raise AcmeError("каталог ACME: HTTP %s" % st)
            self._dir = json.loads(body)
        return self._dir

    def _new_nonce(self):
        _, h, _ = self._http(self._directory()["newNonce"], method="HEAD")
        n = h.get("Replay-Nonce")
        if not n:
            raise AcmeError("ACME не выдал nonce")
        return n

    def _post(self, url, payload, use_jwk=False, accept=None):
        """Подписанный POST (payload=None — POST-as-GET). Повтор на badNonce."""
        for _ in range(3):
            nonce = self._nonce or self._new_nonce()
            self._nonce = None
            prot = {"alg": "ES256", "nonce": nonce, "url": url}
            if use_jwk:
                prot["jwk"] = self._jwk
            else:
                prot["kid"] = self._kid
            p64 = _b64(json.dumps(prot).encode())
            b64 = "" if payload is None else _b64(json.dumps(payload).encode())
            der = self._key.sign(("%s.%s" % (p64, b64)).encode(), ec.ECDSA(hashes.SHA256()))
            r, s = decode_dss_signature(der)
            sig = _b64(r.to_bytes(32, "big") + s.to_bytes(32, "big"))
            body = json.dumps({"protected": p64, "payload": b64, "signature": sig}).encode()
            hdr = {"Content-Type": "application/jose+json"}
            if accept:
                hdr["Accept"] = accept
            st, h, raw = self._http(url, data=body, method="POST", headers=hdr)
            self._nonce = h.get("Replay-Nonce")
            if st >= 400:
                try:
                    err = json.loads(raw)
                except ValueError:
                    err = {"detail": raw[:300].decode("utf-8", "replace")}
                if err.get("type", "").endswith(":badNonce"):
                    continue
                raise AcmeError("%s (HTTP %s, %s)" % (err.get("detail") or "ошибка", st,
                                                      (err.get("type") or "").rsplit(":", 1)[-1]))
            return st, h, raw
        raise AcmeError("ACME: badNonce три раза подряд")

    def _poll(self, url, done, bad, what, timeout=180):
        t_end = time.time() + timeout
        while True:
            _, _, raw = self._post(url, None)
            obj = json.loads(raw)
            st = obj.get("status")
            if st in done:
                return obj
            if st in bad:
                raise AcmeError("%s: %s — %s" % (what, st, _problem(obj)))
            if time.time() > t_end:
                raise AcmeError("%s: не дождались (статус %s)" % (what, st))
            time.sleep(2)

    # --------------------------------------------------------------- выпуск
    def issue(self, domain, log=logging.info):
        """Получить сертификат на ``domain`` и положить в certs/<домен>. Возвращает cert_info."""
        domain = domain.strip().lower()
        if not domain or "/" in domain or ":" in domain:
            raise AcmeError("некорректный домен: %r" % domain)
        with self._lock:
            d = self._directory()
            acc = {"termsOfServiceAgreed": True}
            if self.email:
                acc["contact"] = ["mailto:" + self.email]
            _, h, _ = self._post(d["newAccount"], acc, use_jwk=True)
            self._kid = h.get("Location")
            log("acme: учётная запись %s" % self._kid)

            _, h, raw = self._post(d["newOrder"], {"identifiers": [{"type": "dns", "value": domain}]})
            order_url = h.get("Location")
            order = json.loads(raw)
            tokens = []
            try:
                for az_url in order.get("authorizations", []):
                    _, _, raw = self._post(az_url, None)
                    az = json.loads(raw)
                    if az.get("status") == "valid":
                        continue
                    ch = next((c for c in az.get("challenges", []) if c.get("type") == "http-01"), None)
                    if not ch:
                        raise AcmeError("ACME не предложил проверку http-01")
                    tok = ch["token"]
                    self.challenges[tok] = "%s.%s" % (tok, self._thumb)
                    tokens.append(tok)
                    log("acme: проверка http-01 для %s — жду запрос на "
                        "http://%s/.well-known/acme-challenge/%s" % (domain, domain, tok))
                    self._post(ch["url"], {})
                    self._poll(az_url, {"valid"}, {"invalid", "deactivated", "expired", "revoked"},
                               "проверка домена")
                    log("acme: домен %s подтверждён" % domain)
            finally:
                for tok in tokens:
                    self.challenges.pop(tok, None)

            cert_key = ec.generate_private_key(ec.SECP256R1())
            csr = (x509.CertificateSigningRequestBuilder()
                   .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, domain)]))
                   .add_extension(x509.SubjectAlternativeName([x509.DNSName(domain)]), critical=False)
                   .sign(cert_key, hashes.SHA256()))
            self._post(order["finalize"], {"csr": _b64(csr.public_bytes(serialization.Encoding.DER))})
            order = self._poll(order_url, {"valid"}, {"invalid"}, "выпуск сертификата")
            _, _, pem = self._post(order["certificate"], None, accept="application/pem-certificate-chain")
            if b"BEGIN CERTIFICATE" not in pem:
                raise AcmeError("ACME вернул не сертификат")

            full, keyp = cert_paths(self.base, domain)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            _write_atomic(keyp, cert_key.private_bytes(serialization.Encoding.PEM,
                                                       serialization.PrivateFormat.PKCS8,
                                                       serialization.NoEncryption()))
            _write_atomic(full, pem)
            info = cert_info(self.base, domain)
            log("acme: сертификат для %s получен, действует до %s" % (domain, info.get("not_after")))
            return info


def _problem(obj):
    for c in obj.get("challenges", []) or []:
        e = c.get("error")
        if e:
            return e.get("detail") or str(e)
    e = obj.get("error")
    return (e or {}).get("detail") if isinstance(e, dict) else str(e or "")
