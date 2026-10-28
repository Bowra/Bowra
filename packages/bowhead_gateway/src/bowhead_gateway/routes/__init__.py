"""Gateway 路由集合.

- ``health``    — Liveness / Readiness
- ``chat``      — /api/v1/chat/send + /stream/{turn_id}
- ``memory``    — /api/v1/memory/user*
- ``sessions``  — /api/v1/sessions*
- ``datarc``    — /api/v1/datarc/authorize（前端 iframe W2 鉴权）
- ``files``     — /api/v1/files/upload（附件 stub）
"""
