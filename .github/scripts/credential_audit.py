"""Public repository audit. Matched values are never printed or uploaded."""
from __future__ import annotations
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import tarfile
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import tomli_w

ROOT = Path(os.environ["GITHUB_WORKSPACE"]).resolve()
OUT = Path(os.environ["AUDIT_OUTPUT"]).resolve()
OUT.mkdir(parents=True, exist_ok=True)
RAW = OUT / "private-working-files"
RAW.mkdir(exist_ok=True)
REPO = os.environ["GITHUB_REPOSITORY"]
SCANNER = os.environ["AUDIT_SCANNER"]
API_TOKEN = os.environ["GITHUB_TOKEN"]
ERRORS, FINDINGS = [], []
BASELINES = {"PUSCHi1337/Bose-Homeassistant": "eb11d16902902830ab088b2f1bb8d37d06308138",
             "PUSCHi1337/pybose": "c4fd7665d96d5683e4a6fd18610a95344e9e4f16"}


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        redirected = super().redirect_request(request, fp, code, message, headers, newurl)
        if redirected is not None and urllib.parse.urlsplit(newurl).hostname != "api.github.com":
            redirected.remove_header("Authorization")
        return redirected


OPENER = urllib.request.build_opener(SafeRedirect())


def command(args):
    return subprocess.check_output(args, cwd=ROOT, stderr=subprocess.DEVNULL)


def api(path, binary=False):
    request = urllib.request.Request("https://api.github.com/repos/" + REPO + path,
        headers={"Authorization": "Bearer " + API_TOKEN,
                 "Accept": "application/vnd.github+json", "User-Agent": "public-credential-audit"})
    try:
        with OPENER.open(request, timeout=90) as response:
            data = response.read()
        return data if binary else json.loads(data)
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as error:
        raise RuntimeError("GitHub read failed: " + path.split("?")[0] +
                           " status=" + str(getattr(error, "code", "network"))) from None


def pages(path, key=None):
    result = []
    for number in range(1, 101):
        separator = "&" if "?" in path else "?"
        value = api(path + separator + "per_page=100&page=" + str(number))
        items = value[key] if key else value
        result.extend(items)
        if len(items) < 100:
            return result
    raise RuntimeError("Pagination limit reached")


def write_text(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value or "", encoding="utf-8")


default_bytes = Path(os.environ["AUDIT_DEFAULT_CONFIG"]).read_bytes()
blob = hashlib.sha1(b"blob " + str(len(default_bytes)).encode() + b"\0" + default_bytes).hexdigest()
if blob != os.environ["AUDIT_DEFAULT_CONFIG_BLOB"]:
    raise RuntimeError("Official scanner configuration integrity check failed")
config = tomllib.loads(default_bytes.decode())


def strip_allowlists(value):
    if isinstance(value, dict):
        return {k: strip_allowlists(v) for k, v in value.items()
                if k not in ("allowlist", "allowlists")}
    if isinstance(value, list):
        return [strip_allowlists(v) for v in value]
    return value


config = strip_allowlists(config)
config["rules"].extend([
    {"id": "audit-credential-literal", "description": "Credential-like assignment; review required",
     "regex": r"""(?i)(?:api[_-]?key|password|passwd|passphrase|client[_-]?secret|access[_-]?token|refresh[_-]?token|authorization)\s*["']?\s*(?::\s*(?:str|string))?\s*[:=]\s*["']([^"'\r\n]{4,})["']""",
     "secretGroup": 1,
     "keywords": ["key", "password", "passwd", "passphrase", "secret", "token", "authorization"]},
    {"id": "audit-unquoted-credential", "description": "Unquoted credential; review required",
     "regex": r"(?im)^[ \t]*(?:[A-Z_]*(?:API_KEY|PASSWORD|PASSWD|PASSPHRASE|CLIENT_SECRET|ACCESS_TOKEN|REFRESH_TOKEN))\s*[:=]\s*([A-Za-z0-9][^\s#\r\n]{3,})[ \t]*$",
     "secretGroup": 1, "keywords": ["key", "password", "passwd", "passphrase", "secret", "token"]},
    {"id": "audit-jwt", "description": "JWT-like literal; no online verification",
     "regex": r"eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{20,}",
     "keywords": ["eyJ"]},
    {"id": "audit-private-network", "description": "Private network literal; may be a test/example",
     "regex": r"\b(?:10(?:\.\d{1,3}){3}|192\.168(?:\.\d{1,3}){2}|172\.(?:1[6-9]|2\d|3[01])(?:\.\d{1,3}){2})\b"}
])
CONFIG = RAW / "strict-scanner.toml"
CONFIG.write_text(tomli_w.dumps(config))
IGNORE = RAW / "empty-ignore"
IGNORE.write_text("")


def scan(mode, target, label, extra=(), collect=True):
    raw_report = RAW / (label + "-raw.json")
    args = [SCANNER, mode, str(target), "--no-banner", "--redact=100",
            "--config=" + str(CONFIG), "--gitleaks-ignore-path=" + str(IGNORE),
            "--ignore-gitleaks-allow", "--max-decode-depth=2", "--max-archive-depth=3",
            "--report-format=json", "--report-path=" + str(raw_report), *extra]
    result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode not in (0, 1):
        raise RuntimeError("Scanner did not complete for " + label + " exit=" + str(result.returncode))
    rows = json.loads(raw_report.read_text()) if raw_report.exists() else []
    if raw_report.exists():
        raw_report.unlink()
    if collect:
        for row in rows:
            FINDINGS.append({
                "surface": label, "rule": row.get("RuleID"),
                "file": str(row.get("File", "")).replace(str(RAW), "[RUNNER_WORKDIR]"),
                "start_line": row.get("StartLine"), "end_line": row.get("EndLine"),
                "commit": row.get("Commit"), "date": row.get("Date"),
                "fingerprint_sha256": hashlib.sha256(str(row.get("Fingerprint", "")).encode()).hexdigest(),
            })
    return rows


probe = RAW / "positive-probe"
probe.mkdir()
encode = lambda value: base64.urlsafe_b64encode(value).decode().rstrip("=")
jwt = encode(json.dumps({"alg": "HS256", "typ": "JWT"}).encode()) + "." + \
      encode(json.dumps({"sub": "unregistered-audit-probe", "nonce": secrets.token_hex(16)}).encode()) + "." + \
      encode(secrets.token_bytes(32))
pat = "ghp_" + "".join(secrets.choice("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789") for _ in range(36))
pem_label = " ".join(["RSA", "PRIVATE", "KEY"])
pem = "-----" + "BEGIN " + pem_label + "-----\n" + base64.b64encode(secrets.token_bytes(256)).decode() + \
      "\n-----" + "END " + pem_label + "-----\n"
write_text(probe / "probe.txt", "\n".join([
    'password = "' + secrets.token_urlsafe(24) + '"',
    'api_key = "' + secrets.token_hex(24) + '"',
    'access_token = "' + jwt + '"', 'github_token = "' + pat + '"', pem]))
probe_rules = {row.get("RuleID") for row in scan("dir", probe, "positive-controls", collect=False)}
expected = {"audit-credential-literal", "audit-jwt", "github-pat", "private-key"}
if not expected.issubset(probe_rules):
    raise RuntimeError("Positive controls failed: " + ",".join(sorted(expected - probe_rules)))
# Probes are unregistered synthetic strings, never personal credentials.

fetch = subprocess.run(["git", "fetch", "--no-tags", "origin",
                        "+refs/pull/*/head:refs/remotes/public-pr/*"],
                       cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
if fetch.returncode:
    ERRORS.append({"surface": "pull-request-git-refs", "reason": "fetch failed"})
main_sha = command(["git", "rev-parse", "refs/remotes/origin/main"]).decode().strip()
commits = int(command(["git", "rev-list", "--all", "--count"]).decode())
refs = command(["git", "for-each-ref", "--format=%(refname)"]).decode().splitlines()
historical_paths = sorted(set(command(["git", "log", "--all", "--format=", "--name-only"]).decode(errors="replace").splitlines()))
current_paths = command(["git", "ls-tree", "-r", "--name-only", main_sha]).decode().splitlines()
export = RAW / "current-main"
export.mkdir()
with tarfile.open(fileobj=io.BytesIO(command(["git", "archive", main_sha]))) as archive:
    archive.extractall(export, filter="data")
scan("git", ROOT, "git-history", extra=("--log-opts=--all --full-history -m",))
scan("git", ROOT, "fork-changes", extra=("--log-opts=" + BASELINES[REPO] + ".." + main_sha + " -m",))
scan("dir", export, "current-main")
supplemental = RAW / "public-surfaces"
supplemental.mkdir()
write_text(supplemental / "commit-messages.txt", command(["git", "log", "--all", "--format=%H%n%B"]).decode(errors="replace"))
counts = {"issues_and_pr_bodies": 0, "comments_and_reviews": 0, "completed_ci_runs": 0,
          "ci_job_logs": 0, "artifacts": 0, "artifact_bytes": 0, "expired_artifacts": 0,
          "excluded_audit_or_incomplete_runs": 0}
for issue in pages("/issues?state=all"):
    number = issue["number"]
    write_text(supplemental / ("issue-" + str(number) + "-body.txt"), issue.get("body"))
    counts["issues_and_pr_bodies"] += 1
    if issue.get("comments", 0):
        for comment in pages("/issues/" + str(number) + "/comments"):
            write_text(supplemental / ("issue-comment-" + str(comment["id"]) + ".txt"), comment.get("body"))
            counts["comments_and_reviews"] += 1
    if "pull_request" in issue:
        for review in pages("/pulls/" + str(number) + "/reviews"):
            write_text(supplemental / ("pr-review-" + str(review["id"]) + ".txt"), review.get("body"))
            counts["comments_and_reviews"] += 1
for endpoint, prefix in [("/pulls/comments", "inline-review"), ("/comments", "commit-comment")]:
    for comment in pages(endpoint):
        write_text(supplemental / (prefix + "-" + str(comment["id"]) + ".txt"), comment.get("body"))
        counts["comments_and_reviews"] += 1
for run in pages("/actions/runs", key="workflow_runs"):
    if run["id"] == int(os.environ["GITHUB_RUN_ID"]) or \
       (run.get("head_branch") or "").startswith("audit/credential-check") or run["status"] != "completed":
        counts["excluded_audit_or_incomplete_runs"] += 1
        continue
    counts["completed_ci_runs"] += 1
    try:
        jobs = pages("/actions/runs/" + str(run["id"]) + "/jobs?filter=all", key="jobs")
    except RuntimeError:
        ERRORS.append({"surface": "ci-run", "id": run["id"], "reason": "job listing unavailable"})
        continue
    for job in jobs:
        if job["status"] != "completed":
            continue
        try:
            data = api("/actions/jobs/" + str(job["id"]) + "/logs", binary=True)
            (supplemental / ("ci-run-" + str(run["id"]) + "-job-" + str(job["id"]) + ".log")).write_bytes(data)
            counts["ci_job_logs"] += 1
        except RuntimeError:
            ERRORS.append({"surface": "ci-job", "id": job["id"], "reason": "logs unavailable"})
for artifact in pages("/actions/artifacts", key="artifacts"):
    if artifact["expired"]:
        counts["expired_artifacts"] += 1
        continue
    if artifact["size_in_bytes"] > 512 * 1024 * 1024:
        ERRORS.append({"surface": "artifact", "id": artifact["id"], "reason": "over 512 MiB"})
        continue
    try:
        data = api("/actions/artifacts/" + str(artifact["id"]) + "/zip", binary=True)
        (supplemental / ("artifact-" + str(artifact["id"]) + ".zip")).write_bytes(data)
        counts["artifacts"] += 1
        counts["artifact_bytes"] += len(data)
    except RuntimeError:
        ERRORS.append({"surface": "artifact", "id": artifact["id"], "reason": "download unavailable"})
scan("dir", supplemental, "public-messages-logs-artifacts")
suspicious = re.compile(r"(^|/)(?:\.env(?:$|\.)|secrets?\.(?:ya?ml|json)|credentials?(?:\.|/)|id_rsa|\.storage/|home-assistant\.log)|\.(?:pem|p12|pfx|key)$", re.I)
report = {
    "repository": REPO, "checked_main": main_sha, "baseline": BASELINES[REPO],
    "audit_commit": os.environ["GITHUB_SHA"], "scanner_version": "8.30.1",
    "default_rule_count": len(config["rules"]) - 4, "custom_rule_count": 4,
    "positive_controls": sorted(expected), "all_scanner_allowlists_removed": True,
    "inline_allow_comments_ignored": True, "decode_depth": 2, "archive_depth": 3,
    "git_commits": commits, "git_refs": len(refs), "current_main_files": len(current_paths),
    "historical_unique_paths": len([p for p in historical_paths if p]),
    "credential_related_current_paths": [p for p in current_paths if suspicious.search(p)],
    "credential_related_historical_paths": [p for p in historical_paths if suspicious.search(p)],
    "public_surfaces": counts, "coverage_errors": ERRORS, "candidate_count": len(FINDINGS),
    "review_required": bool(FINDINGS), "findings": FINDINGS, "values_included": False,
    "credential_verification_performed": False,
    "limits": ["Discussion-body edit history is not part of this API export.",
               "Images are not OCR-scanned; unsupported binary formats are not semantically decoded.",
               "Expired/deleted artifacts or logs cannot be recovered."]
}
(OUT / "redacted-audit.json").write_text(json.dumps(report, indent=2) + "\n")
print(json.dumps({k: report[k] for k in ["repository", "checked_main", "git_commits", "git_refs",
    "current_main_files", "public_surfaces", "candidate_count", "review_required", "coverage_errors",
    "values_included", "credential_verification_performed"]}))
