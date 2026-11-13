#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Automated Deployment Client for Render Cloud Services
═════════════════════════════════════════════════════
Milestone: M3 (Feature 18 Cloud Automated Deployment)
Location: scripts/deploy_render.py

Features:
1. Calls Render REST API v1 (https://api.render.com/v1) using standard library urllib.
2. Supports actions: list, status, create, deploy, and update-env.
3. Automatically maps ownerId (tea-dbr81smtlv9c73fo1uk0) and RENDER_API_KEY.
4. Reads .env and securely injects credentials as Render service environment variables.
5. Configures Docker runtime, health check path (/api/state), and 750h free tier plan.
"""

import argparse
import json
import logging
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("deploy_render")

# Default credentials provided for user's workspace
DEFAULT_RENDER_API_KEY = "rnd_jb6f6LpJYzUK0F5GFiQ0MurWHHCD"
DEFAULT_RENDER_OWNER_ID = "tea-dbr81smtlv9c73fo1uk0"
RENDER_API_BASE_URL = "https://api.render.com/v1"

def setup_logging(level: str = "INFO"):
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] [render] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

def mask_secret(secret: str) -> str:
    if len(secret) <= 8:
        return "********"
    return f"{secret[:4]}...{secret[-4:]}"

# ==============================================================================
# 1. Render REST API Client
# ==============================================================================
class RenderAPIClient:
    """Wrapper around Render REST API v1."""
    def __init__(self, api_key: str, base_url: str = RENDER_API_BASE_URL, dry_run: bool = False):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.dry_run = dry_run

    def _request(self, method: str, endpoint: str, params: Optional[Dict[str, Any]] = None, data: Optional[Any] = None) -> Tuple[int, Any]:
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        if params:
            query_str = urllib.parse.urlencode(params)
            url = f"{url}?{query_str}"

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
            "User-Agent": "TradingBotRenderDeployer/1.0"
        }

        body_bytes = None
        if data is not None:
            headers["Content-Type"] = "application/json"
            body_bytes = json.dumps(data).encode("utf-8")

        if self.dry_run and method.upper() in ("POST", "PUT", "PATCH", "DELETE"):
            logger.info("[DRY_RUN] %s %s -> Data: %s", method.upper(), url, data)
            return 200, {"dry_run": True, "method": method, "endpoint": endpoint, "payload": data}

        req = urllib.request.Request(url, data=body_bytes, headers=headers, method=method.upper())
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                status_code = resp.getcode()
                raw_resp = resp.read().decode("utf-8")
                parsed_json = json.loads(raw_resp) if raw_resp else {}
                return status_code, parsed_json
        except urllib.error.HTTPError as e:
            raw_err = e.read().decode("utf-8")
            try:
                err_json = json.loads(raw_err)
            except Exception:
                err_json = {"raw_error": raw_err}
            logger.error("[API_ERROR] %s %s returned HTTP %d: %s", method, url, e.code, err_json)
            return e.code, err_json
        except Exception as e:
            logger.error("[NETWORK_ERROR] Request to %s failed: %s", url, e)
            return 0, {"error": str(e)}

    def list_owners(self) -> List[Dict[str, Any]]:
        status, data = self._request("GET", "owners")
        return data if isinstance(data, list) else []

    def list_services(self, owner_id: Optional[str] = None) -> List[Dict[str, Any]]:
        params = {"limit": 50}
        if owner_id:
            params["ownerId"] = owner_id
        status, data = self._request("GET", "services", params=params)
        return data if isinstance(data, list) else []

    def get_service(self, service_id: str) -> Dict[str, Any]:
        status, data = self._request("GET", f"services/{service_id}")
        return data if isinstance(data, dict) else {}

    def create_web_service(
        self,
        owner_id: str,
        name: str,
        repo: str,
        branch: str = "main",
        dockerfile_path: str = "Dockerfile",
        health_check_path: str = "/api/state",
        plan: str = "free",
        region: str = "oregon",
        env_vars: Optional[List[Dict[str, str]]] = None,
        auto_deploy: str = "no"
    ) -> Tuple[bool, Dict[str, Any]]:
        """Creates a Web Service on Render using Docker runtime."""
        payload = {
            "type": "web_service",
            "name": name,
            "ownerId": owner_id,
            "repo": repo,
            "autoDeploy": auto_deploy,
            "branch": branch,
            "rootDir": "",
            "serviceDetails": {
                "env": "docker",
                "plan": plan,
                "region": region,
                "dockerfilePath": dockerfile_path,
                "healthCheckPath": health_check_path,
                "envVars": env_vars or []
            }
        }
        status, resp = self._request("POST", "services", data=payload)
        success = (status in (200, 201)) or (self.dry_run and status == 200)
        return success, resp

    def trigger_deploy(self, service_id: str, clear_cache: bool = False) -> Tuple[bool, Dict[str, Any]]:
        """Triggers a manual deployment for the given service."""
        payload = {
            "clearCache": "clear" if clear_cache else "do_not_clear"
        }
        status, resp = self._request("POST", f"services/{service_id}/deploys", data=payload)
        success = (status in (200, 201)) or (self.dry_run and status == 200)
        return success, resp

    def list_deploys(self, service_id: str, limit: int = 5) -> List[Dict[str, Any]]:
        status, resp = self._request("GET", f"services/{service_id}/deploys", params={"limit": limit})
        return resp if isinstance(resp, list) else []

    def update_env_vars(self, service_id: str, env_vars: List[Dict[str, str]]) -> Tuple[bool, Dict[str, Any]]:
        """Updates environment variables for an existing service."""
        status, resp = self._request("PUT", f"services/{service_id}/env-vars", data=env_vars)
        success = (status in (200, 201)) or (self.dry_run and status == 200)
        return success, resp

# ==============================================================================
# 2. Environment Variables Extraction
# ==============================================================================
def load_env_vars(env_file: Optional[str] = None) -> List[Dict[str, str]]:
    """Loads key-value pairs from .env or active process environment."""
    env_vars: Dict[str, str] = {
        "PORT": "10000",
        "MAINTENANCE_LOCK": "false",
        "MODEL_MACRO": "deepseek-chat",
        "MODEL_AI_TRADE": "deepseek-reasoner",
        "MAIL_ENABLED": "false"
    }

    target_env = env_file or (".env" if os.path.exists(".env") else None)
    if target_env and os.path.exists(target_env):
        logger.info("[ENV] Reading environment variables from %s...", target_env)
        try:
            with open(target_env, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        k = k.strip()
                        v = v.strip().strip('"').strip("'")
                        if k:
                            env_vars[k] = v
        except Exception as e:
            logger.warning("[ENV] Failed to parse %s: %s", target_env, e)

    # Explicit whitelist of keys for trading bot
    critical_keys = [
        "HYPE_USER_ADDRESS",
        "HYPE_SECRET_KEY",
        "HYPE_API_WALLET_ADDRESS",
        "HYPE_COIN",
        "MAINTENANCE_LOCK",
        "PORT",
        "THIRD_PARTY_BASE_URL",
        "THIRD_PARTY_API_KEY",
        "MODEL_MACRO",
        "MODEL_AI_TRADE",
        "MAIL_ENABLED",
        "SMTP_SERVER",
        "SMTP_PORT",
        "SMTP_USER",
        "SMTP_PASSWORD",
        "MAIL_TO",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHAT_ID"
    ]

    result = []
    for k in critical_keys:
        val = env_vars.get(k) or os.getenv(k, "")
        if val:
            result.append({"key": k, "value": val})

    return result

# ==============================================================================
# 3. CLI Parser & Command Handlers
# ==============================================================================
def parse_args():
    parser = argparse.ArgumentParser(
        description="Automated Render Cloud Deployment & Management Client",
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--action",
        choices=["list", "status", "create", "deploy", "update-env", "owners"],
        default="list",
        help="Action to execute on Render API"
    )
    parser.add_argument(
        "--api-key",
        default=os.getenv("RENDER_API_KEY", DEFAULT_RENDER_API_KEY),
        help="Render API Bearer Token"
    )
    parser.add_argument(
        "--owner-id",
        default=os.getenv("RENDER_OWNER_ID", DEFAULT_RENDER_OWNER_ID),
        help="Render Workspace Owner ID"
    )
    parser.add_argument("--service-id", help="Render Service ID (srv-...)")
    parser.add_argument("--name", default="hyperliquid-trading-bot", help="Web Service name")
    parser.add_argument("--repo", default="", help="Git repository URL for building (e.g. https://github.com/user/repo)")
    parser.add_argument("--branch", default="main", help="Git branch to track")
    parser.add_argument("--plan", default="free", choices=["free", "starter", "standard", "pro"], help="Compute plan")
    parser.add_argument("--region", default="oregon", choices=["oregon", "frankfurt", "ohio", "singapore", "virginia"], help="Render datacenter region")
    parser.add_argument("--dockerfile-path", default="Dockerfile", help="Path to Dockerfile inside repo")
    parser.add_argument("--health-check-path", default="/api/state", help="Health check endpoint for keepalive")
    parser.add_argument("--env-file", default=".env", help="Path to local .env file")
    parser.add_argument("--clear-cache", action="store_true", help="Clear build cache when triggering deploy")
    parser.add_argument("--dry-run", action="store_true", help="Simulate API calls without mutating cloud resources")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"], help="Log verbosity")

    return parser.parse_args()

def main():
    args = parse_args()
    setup_logging(args.log_level)

    client = RenderAPIClient(api_key=args.api_key, dry_run=args.dry_run)

    # 1. Action: owners
    if args.action == "owners":
        logger.info("[CLI] Querying Render workspaces...")
        owners = client.list_owners()
        print(json.dumps(owners, indent=2))
        sys.exit(0)

    # 2. Action: list
    elif args.action == "list":
        logger.info("[CLI] Querying services for owner: %s...", args.owner_id)
        services = client.list_services(owner_id=args.owner_id)
        print(f"Found {len(services)} service(s):")
        for s in services:
            svc = s.get("service", {})
            srv_id = svc.get("id")
            name = svc.get("name")
            svc_type = svc.get("type")
            url = svc.get("serviceDetails", {}).get("url", "no-url")
            print(f"  - [{srv_id}] {name} ({svc_type}) -> {url}")
        if not services:
            print("  (No services found in this workspace)")
        sys.exit(0)

    # 3. Action: status
    elif args.action == "status":
        if not args.service_id:
            # Attempt to find by name
            services = client.list_services(owner_id=args.owner_id)
            for s in services:
                svc = s.get("service", {})
                if svc.get("name") == args.name:
                    args.service_id = svc.get("id")
                    break

        if not args.service_id:
            logger.error("[CLI] --service-id is required for 'status' action (or provide matching --name).")
            sys.exit(1)

        logger.info("[CLI] Retrieving details for service: %s...", args.service_id)
        details = client.get_service(args.service_id)
        deploys = client.list_deploys(args.service_id, limit=3)

        summary = {
            "service": details.get("service", details),
            "recent_deploys": deploys
        }
        print(json.dumps(summary, indent=2))
        sys.exit(0)

    # 4. Action: create
    elif args.action == "create":
        if not args.repo:
            logger.error("[CLI] --repo (Git repository URL) is required to create a Render Web Service.")
            sys.exit(1)

        env_vars = load_env_vars(args.env_file)
        logger.info("[CLI] Creating Web Service '%s' on Render...", args.name)
        logger.info("[CLI] Owner ID: %s | Plan: %s | Region: %s", args.owner_id, args.plan, args.region)
        logger.info("[CLI] Injecting %d environment variables from %s...", len(env_vars), args.env_file)

        success, resp = client.create_web_service(
            owner_id=args.owner_id,
            name=args.name,
            repo=args.repo,
            branch=args.branch,
            dockerfile_path=args.dockerfile_path,
            health_check_path=args.health_check_path,
            plan=args.plan,
            region=args.region,
            env_vars=env_vars
        )

        if success:
            logger.info("[CLI] Service created successfully!")
            print(json.dumps(resp, indent=2))
            sys.exit(0)
        else:
            logger.error("[CLI] Service creation failed!")
            print(json.dumps(resp, indent=2))
            sys.exit(1)

    # 5. Action: deploy
    elif args.action == "deploy":
        if not args.service_id:
            services = client.list_services(owner_id=args.owner_id)
            for s in services:
                svc = s.get("service", {})
                if svc.get("name") == args.name:
                    args.service_id = svc.get("id")
                    break

        if not args.service_id:
            logger.error("[CLI] --service-id is required to trigger deployment.")
            sys.exit(1)

        logger.info("[CLI] Triggering deployment for service: %s (clear_cache=%s)...", args.service_id, args.clear_cache)
        success, resp = client.trigger_deploy(args.service_id, clear_cache=args.clear_cache)
        if success:
            logger.info("[CLI] Deployment initiated successfully!")
            print(json.dumps(resp, indent=2))
            sys.exit(0)
        else:
            logger.error("[CLI] Deployment trigger failed!")
            print(json.dumps(resp, indent=2))
            sys.exit(1)

    # 6. Action: update-env
    elif args.action == "update-env":
        if not args.service_id:
            logger.error("[CLI] --service-id is required for 'update-env' action.")
            sys.exit(1)

        env_vars = load_env_vars(args.env_file)
        logger.info("[CLI] Updating environment variables for service %s (%d variables)...", args.service_id, len(env_vars))
        success, resp = client.update_env_vars(args.service_id, env_vars)
        if success:
            logger.info("[CLI] Environment variables updated successfully!")
            sys.exit(0)
        else:
            logger.error("[CLI] Environment variable update failed!")
            print(json.dumps(resp, indent=2))
            sys.exit(1)

if __name__ == "__main__":
    main()
