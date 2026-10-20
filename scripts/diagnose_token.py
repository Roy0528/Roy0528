"""檢查 STATS_TOKEN 在各種查詢方式下看得到幾個 repo，用來找出 private repo 沒被算進去的原因。

repo 是公開的，Action 紀錄任何人都看得到，所以這裡只印數量和 token 類型，
不印 token 內容，也不印任何 repo 名稱。

Example:
    STATS_TOKEN=xxx python scripts/diagnose_token.py --user Roy0528
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

from generate_stats import run_query

REST_URL = "https://api.github.com"

# 只判斷公開的前綴格式，不碰前綴之後的任何字元
TOKEN_KINDS = {
    "github_pat_": "fine-grained PAT",
    "ghp_": "classic PAT",
    "gho_": "OAuth token",
    "ghs_": "GitHub Actions / App token",
}

GRAPHQL_CHECKS = {
    "viewer.login": "query { viewer { login } }",
    "user(login).repositories OWNER": (
        'query($login: String!) { user(login: $login) { repositories(ownerAffiliations: OWNER) { totalCount } } }'
    ),
    "user(login).repositories OWNER PRIVATE": (
        'query($login: String!) { user(login: $login) '
        "{ repositories(ownerAffiliations: OWNER, privacy: PRIVATE) { totalCount } } }"
    ),
    "viewer.repositories OWNER": "query { viewer { repositories(ownerAffiliations: OWNER) { totalCount } } }",
    "viewer.repositories OWNER PRIVATE": (
        "query { viewer { repositories(ownerAffiliations: OWNER, privacy: PRIVATE) { totalCount } } }"
    ),
}


def describe_token(token: str) -> str:
    """依公開的前綴格式判斷 token 類型。"""
    for prefix, kind in TOKEN_KINDS.items():
        if token.startswith(prefix):
            return kind
    return "unknown format"


def rest_count(token: str, path: str) -> str:
    """呼叫 REST API，回傳清單長度或錯誤代碼。

    Args:
        token: GitHub token。
        path: API 路徑（含查詢字串）。

    Returns:
        給人看的結果字串。
    """
    request = urllib.request.Request(
        REST_URL + path,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "profile-stats-diagnose",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = json.load(response)
    except urllib.error.HTTPError as error:
        return f"HTTP {error.code}"
    return f"{len(body)} items" if isinstance(body, list) else "not a list"


def graphql_result(token: str, query: str, login: str) -> str:
    """執行一個 GraphQL 檢查，只回傳 login 或 totalCount。"""
    variables = {"login": login} if "$login" in query else {}
    try:
        data = run_query(token, query, variables)
    except urllib.error.HTTPError as error:
        return f"HTTP {error.code}"
    except RuntimeError as error:
        return f"GraphQL error: {str(error)[:200]}"
    node = data.get("viewer") or data.get("user") or {}
    if "login" in node:
        return "matches --user" if node["login"].lower() == login.lower() else "different account"
    return f"totalCount={node['repositories']['totalCount']}"


def main() -> int:
    """命令列進入點。"""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--user", required=True, help="GitHub 帳號")
    args = parser.parse_args()

    token = os.environ.get("STATS_TOKEN", "")
    if not token:
        print("STATS_TOKEN: not set")
        return 0

    print(f"STATS_TOKEN kind: {describe_token(token)}")
    for label, query in GRAPHQL_CHECKS.items():
        print(f"GraphQL {label}: {graphql_result(token, query, args.user)}")
    print(f"REST /user/repos owner private: {rest_count(token, '/user/repos?visibility=private&affiliation=owner&per_page=100')}")
    print(f"REST /user/repos owner all: {rest_count(token, '/user/repos?affiliation=owner&per_page=100')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
