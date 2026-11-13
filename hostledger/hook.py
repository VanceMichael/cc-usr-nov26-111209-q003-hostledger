def handle(server, path, headers):
    """本地来源扩展点：返回 (status, content_type, body) 即可截获请求，返回 None 走固定路由。"""
    if path.startswith('/control/phase'):
        return None
    return None
