"""登录 / 注册 / 用户中心 / 登出 的请求处理（由 http.py 分发调用）

CSRF 统一在分发器（http._route_post）校验后才进入本模块，
因此这里只负责业务分支；视图侧拿到的 form_data 已经是"令牌已验证"的。
"""
from .http_base import html_response, redirect_response
from .security import clear_session_cookie, set_cookie_header
from .db_session import delete_session, delete_user_sessions
from . import view_login, view_register, view_user


# ----- GET -----
def auth_login_get(ctx):
    token = ctx.ensure_csrf()
    return html_response(view_login.render(user=ctx.user, csrf_token=token,
                                           theme=ctx.theme, path=ctx.path, lang=ctx.lang))


def auth_register_get(ctx):
    token = ctx.ensure_csrf()
    return html_response(view_register.render(user=ctx.user, csrf_token=token,
                                              theme=ctx.theme, path=ctx.path, lang=ctx.lang))


def auth_user_get(ctx):
    # 仅登录用户页面包含表单，未登录时不生成 CSRF Cookie
    token = ctx.ensure_csrf() if ctx.user else None
    return html_response(view_user.render(user=ctx.user, csrf_token=token,
                                          theme=ctx.theme, path=ctx.path, lang=ctx.lang))


# ----- POST -----
def auth_login_post(ctx):
    # 无论成败都确保令牌存在：错误页/重定向下发的表单与 Cookie 保持一致
    token = ctx.ensure_csrf()
    result = view_login.render(
        request_method="POST", form_data=ctx.form, user=ctx.user,
        client_ip=ctx.client_ip, csrf_token=token, csrf_ok=True,
        theme=ctx.theme, path=ctx.path, lang=ctx.lang,
    )
    if isinstance(result, tuple) and result[0] == "redirect":
        _, target, session_token = result
        headers = []
        if session_token:
            headers.append(set_cookie_header(session_token, secure=ctx.secure))
        return redirect_response(target, headers=headers)
    return html_response(result)


def auth_register_post(ctx):
    token = ctx.ensure_csrf()
    result = view_register.render(
        request_method="POST", form_data=ctx.form, user=ctx.user, csrf_token=token,
        client_ip=ctx.client_ip, csrf_ok=True,
        theme=ctx.theme, path=ctx.path, lang=ctx.lang,
    )
    if isinstance(result, tuple) and result[0] == "redirect":
        _, target, _session_token = result
        return redirect_response(target)
    return html_response(result)


def auth_user_post(ctx):
    if not ctx.user:
        # 未登录时 /user 没有可提交的状态变更：不生成 CSRF Cookie，只回渲染结果
        return html_response(view_user.render(user=None, theme=ctx.theme,
                                              path=ctx.path, lang=ctx.lang))
    token = ctx.ensure_csrf()
    result = view_user.render(
        request_method="POST", form_data=ctx.form, user=ctx.user, csrf_token=token,
        csrf_ok=True, theme=ctx.theme, path=ctx.path, lang=ctx.lang,
    )
    if isinstance(result, tuple):
        kind = result[0]
        if kind == "password_changed":
            # 清除该用户所有会话并移除浏览器会话 Cookie，强制重新登录
            delete_user_sessions(ctx.user["id"])
            return redirect_response("/login", headers=[clear_session_cookie(secure=ctx.secure)])
        if kind == "redirect_logout":
            # 账号已逻辑删除、会话行已清空；移除浏览器会话 Cookie 后回首页
            return redirect_response("/", headers=[clear_session_cookie(secure=ctx.secure)])
    return html_response(result)


def auth_logout_post(ctx):
    if ctx.session_token:
        delete_session(ctx.session_token)
    return redirect_response("/", headers=[clear_session_cookie(secure=ctx.secure)])
