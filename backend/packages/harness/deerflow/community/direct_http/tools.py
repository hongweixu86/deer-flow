"""Direct HTTP Tools - 直接发起 HTTP 请求，不经过第三方代理"""

import json
import logging
from typing import Any

import requests
from langchain.tools import tool
from urllib.parse import urlparse,parse_qsl

logger = logging.getLogger(__name__)


@tool("http_request", parse_docstring=True)
def http_request_tool(
    url: str,
    method: str = "GET",
    headers: dict | None = None,
    data: dict | None = None,
    timeout: int = 30,
) -> str:
    """直接发起 HTTP/HTTPS 请求，支持内网地址（127.0.0.1, 192.168.x.x, 10.x.x.x 等）。
    
    适用于:
    - 访问内网 API 接口
    - 访问本地服务 (localhost/127.0.0.1)
    - 直接获取网页内容（无需 Jina 等代理）
    
    注意: 此工具直接发起请求，不经过第三方代理服务。
    
    Args:
        url: 完整的请求 URL，如 "http://192.168.1.100:8080/api/data"
        method: HTTP 方法，支持 GET/POST/PUT/DELETE/PATCH，默认 GET
        headers: 请求头字典，如 {"Authorization": "Bearer xxx", "Content-Type": "application/json"}
        data: 请求体数据（用于 POST/PUT/PATCH），字典格式
        timeout: 超时时间（秒），默认 30
    """
    try:
        # 准备请求
        request_headers = {
            "User-Agent": "DeerFlow-HTTP-Client/1.0",
        }
        if headers:
            request_headers.update(headers)
        
        parsed = urlparse(url)
        query_params = dict(parse_qsl(parsed.query))


        # 发起请求
        response = requests.request(
            method=method.upper(),
            url=f"{parsed.scheme}://{parsed.netloc}{parsed.path}",
            headers=request_headers,
            json=data if data else None,
            timeout=timeout,
            allow_redirects=True,
            params=query_params
        )
        
        # 构建返回结果
        result = {
            "success": True,
            "status_code": response.status_code,
            "url": response.url,
            "headers": dict(response.headers),
        }
        
        # 尝试解析响应体
        content_type = response.headers.get("Content-Type", "")
        try:
            if "application/json" in content_type:
                result["data"] = response.json()
            else:
                # 限制返回内容长度，避免 Token 爆炸
                text = response.text
                if len(text) > 10000:
                    text = text[:10000] + f"\n... [截断: 共 {len(text)} 字符] ..."
                result["text"] = text
        except Exception as e:
            result["text"] = f"[无法解析响应体: {e}]"
        
        return json.dumps(result, indent=2, ensure_ascii=False)
        
    except requests.exceptions.Timeout:
        return json.dumps({
            "success": False,
            "error": f"请求超时（{timeout}秒）",
            "url": url,
        }, ensure_ascii=False)
    except requests.exceptions.ConnectionError as e:
        return json.dumps({
            "success": False,
            "error": f"连接失败: {str(e)}",
            "url": url,
            "hint": "请检查: 1) 目标地址是否可访问 2) 端口是否正确 3) 防火墙设置",
        }, ensure_ascii=False)
    except Exception as e:
        logger.error(f"HTTP 请求失败 [{url}]: {e}")
        return json.dumps({
            "success": False,
            "error": f"请求异常: {str(e)}",
            "url": url,
        }, ensure_ascii=False)


@tool("fetch_url", parse_docstring=True)
def fetch_url_tool(
    url: str,
    max_length: int = 5000,
) -> str:
    """获取 URL 内容（简化版），用于抓取网页或 API 返回。
    
    这是 web_fetch 的直连替代品，支持内网地址。
    
    Args:
        url: 完整的 URL，如 "http://192.168.1.100:8080/page" 或 "https://example.com"
        max_length: 最大返回长度，默认 5000 字符
    """
    try:
        response = requests.get(
            url=url,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.0",
            },
            timeout=30,
        )
        response.raise_for_status()
        
        content_type = response.headers.get("Content-Type", "")
        
        # JSON 响应
        if "application/json" in content_type:
            data = response.json()
            return json.dumps({
                "success": True,
                "type": "json",
                "data": data,
            }, indent=2, ensure_ascii=False)[:max_length]
        
        # HTML/文本响应
        text = response.text
        if len(text) > max_length:
            text = text[:max_length] + f"\n... [截断: 共 {len(response.text)} 字符] ..."
        
        return json.dumps({
            "success": True,
            "type": "text",
            "content_type": content_type,
            "text": text,
        }, indent=2, ensure_ascii=False)
        
    except Exception as e:
        return json.dumps({
            "success": False,
            "error": str(e),
            "url": url,
        }, ensure_ascii=False)
