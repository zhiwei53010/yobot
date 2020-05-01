import json
import os
import time
from hashlib import sha256
from typing import Union
from urllib.parse import urljoin

from aiocqhttp.api import Api
from apscheduler.triggers.cron import CronTrigger
from quart import (Quart, Response, jsonify, make_response, redirect, request,
                   session, url_for)

from .templating import render_template
from .web_util import rand_string
from .ybdata import User, User_login

EXPIRED_TIME = 7 * 24 * 60 * 60  # 7 days
LOGIN_AUTH_COOKIE_NAME = 'yobot_login'


class ExceptionWithAdvice(RuntimeError):

    def __init__(self, reason: str, advice=''):
        super(ExceptionWithAdvice, self).__init__(reason)
        self.reason = reason
        self.advice = advice


def _add_salt_and_hash(raw: str, salt: str):
    return sha256((raw + salt).encode()).hexdigest()


class Login:
    Passive = True
    Active = True
    Request = True

    def __init__(self,
                 glo_setting,
                 bot_api: Api,
                 *args, **kwargs):
        self.setting = glo_setting
        self.api = bot_api

    def jobs(self):
        trigger = CronTrigger(hour=5)
        return ((trigger, self.drop_expired_logins),)

    def drop_expired_logins(self):
        # 清理过期cookie
        now = int(time.time())
        User_login.delete().where(
            User_login.auth_cookie_expire_time < now,
        ).execute()

    @staticmethod
    def match(cmd: str):
        cmd = cmd.split(' ')[0]
        if cmd in ['登录', '登陆']:
            return 1
        return 0

    def execute(self, match_num: int, ctx: dict) -> dict:
        if ctx['message_type'] != 'private':
            return {
                'reply': '请私聊使用',
                'block': True
            }

        login_code = rand_string(6)

        user = self._get_or_create_user_model(ctx)
        user.login_code = login_code
        user.login_code_available = True
        user.login_code_expire_time = int(time.time())+60
        user.save()

        # 链接登录
        newurl = urljoin(
            self.setting['public_address'],
            '{}login/?qqid={}&key={}'.format(
                self.setting['public_basepath'],
                user.qqid,
                login_code,
            )
        )
        reply = newurl+'#\n请在一分钟内点击'
        # if self.setting['web_mode_hint']:
        #     reply += '\n\n如果连接无法打开，请仔细阅读教程中《链接无法打开》的说明'

        return {
            'reply': reply,
            'block': True
        }

    def _get_or_create_user_model(self, ctx: dict) -> User:
        if not self.setting['super-admin']:
            authority_group = 1
            self.setting['super-admin'].append(ctx['user_id'])
            save_setting = self.setting.copy()
            del save_setting['dirname']
            del save_setting['verinfo']
            config_path = os.path.join(
                self.setting['dirname'], 'yobot_config.json')
            with open(config_path, 'w', encoding='utf-8') as f:
                json.dump(save_setting, f, indent=4)
        elif ctx['user_id'] in self.setting['super-admin']:
            authority_group = 1
        else:
            authority_group = 100

        # 取出数据
        return User.get_or_create(
            qqid=ctx['user_id'],
            defaults={
                'nickname': ctx['sender']['nickname'],
                'authority_group': authority_group,
            }
        )[0]

    # 这个放到前端
    # @staticmethod
    # def _validate_pwd(pwd: str) -> Union[str, bool]:
    #     """
    #     验证用户密码是否合乎硬性条件
    #     :return: 合法返回True，不合法抛出ValueError异常
    #     """
    #     if len(pwd) < 8:
    #         raise ValueError('密码至少需要8位')
    #     char_regex = re.compile(
    #         r'^[0-9a-zA-Z!\-\\/@#$%^&*?_.()+=\[\]{}|;:<>`~]+$')
    #     if not char_regex.match(pwd):
    #         raise ValueError('密码不能含有中文或密码中含有特殊符号')
    #     return True

    def _get_prefix(self):
        return self.setting['preffix_string'] if self.setting['preffix_on'] else ''

    def _check_pwd(self, user: User, pwd: str) -> bool:
        """
        检查是否设置密码且密码是否正确，
        :return: 成功返回True，失败抛出异常。
        """
        if not user or not user.password or not user.salt:
            raise ExceptionWithAdvice(
                'QQ号错误 或 您尚未设置密码',
                f'请私聊机器人“{self._get_prefix()}登录”后，再次选择[修改密码]修改'
            )
        if user.privacy >= 3:
            raise ExceptionWithAdvice(
                '您的密码错误次数过多，账号已锁定',
                f'如果忘记密码，请私聊机器人“{self._get_prefix()}登录”后，再次选择[修改密码]修改'
            )
        if not user.password == _add_salt_and_hash(pwd, user.salt):
            user.privacy += 1  # 密码错误次数+1
            user.save()
            raise ExceptionWithAdvice(
                '您的密码不正确',
                f'如果忘记密码，请私聊机器人“{self._get_prefix()}登录”后，再次选择[修改密码]修改'
            )
        return True

    def _check_key(self, user: User, key: str) -> Union[bool, str]:
        """
        检查登录码是否正确且在有效期内
        :return: 成功返回True，失败抛出异常
        """
        now = int(time.time())
        if user is None or user.login_code != key:
            # 登录码错误
            raise ExceptionWithAdvice(
                '无效的登录地址',
                f'请检查登录地址是否完整且为最新。'
            )
        if user.login_code_expire_time < now:
            # 登录码正确但超时
            raise ExceptionWithAdvice(
                '这个登录地址已过期',
                f'私聊机器人“{self._get_prefix()}登录”获取新登录地址'
            )
        if not user.login_code_available:
            # 登录码正确但已被使用
            raise ExceptionWithAdvice(
                '这个登录地址已被使用',
                f'私聊机器人“{self._get_prefix()}登录”获取新登录地址'
            )
        return True

    def _recall_from_cookie(self, auth_cookie) -> User:
        """
        检测cookie中的登录状态是否正确，如果cookie有误 会抛出异常
        :return User: 返回找回的user对象
        """
        advice = f'请私聊机器人“{self._get_prefix()}登录”或重新登录'
        if not auth_cookie:
            raise ExceptionWithAdvice('登录已过期', advice)
        s = auth_cookie.split(':')
        if len(s) != 2:
            raise ExceptionWithAdvice('Cookie异常', advice)
        qqid, auth = s

        user = User.get_or_none(User.qqid == qqid)
        advice = f'请先加入一个公会 或 私聊机器人“{self._get_prefix()}登录”'
        if user is None:
            # 有有效Cookie但是数据库没有，怕不是删库跑路了
            raise ExceptionWithAdvice('用户不存在', advice)
        salty_cookie = _add_salt_and_hash(auth, user.salt)
        userlogin = User_login.get_or_none(
            qqid=qqid,
            auth_cookie=salty_cookie,
        )
        if userlogin is None:
            raise ExceptionWithAdvice('Cookie异常', advice)
        now = int(time.time())
        if userlogin.auth_cookie_expire_time < now:
            raise ExceptionWithAdvice('登录已过期', advice)

        userlogin.last_login_time = now
        userlogin.last_login_ipaddr = request.headers.get(
            'X-Real-IP', request.remote_addr)
        userlogin.save()

        return user

    @staticmethod
    def _set_auth_info(user: User, res: Response = None, save_user=True):
        """
        为某用户设置session中的授权信息
        并自动修改中的上次登录的信息
        :param user: 用户模型
        :param save_user: 是否自动执行user.save()
        :param res: 如果需要自动更新cookie，请传入返回的response
        """
        now = int(time.time())
        session['yobot_user'] = user.qqid
        session['csrf_token'] = rand_string(16)
        session['last_login_time'] = user.last_login_time
        session['last_login_ipaddr'] = user.last_login_ipaddr
        user.last_login_time = now
        user.last_login_ipaddr = request.headers.get(
            'X-Real-IP', request.remote_addr)
        if res:
            new_key = rand_string(32)
            userlogin = User_login.create(
                qqid=user.qqid,
                auth_cookie=_add_salt_and_hash(new_key, user.salt),
                auth_cookie_expire_time=now + EXPIRED_TIME,
            )
            new_cookie = f'{user.qqid}:{new_key}'
            res.set_cookie(LOGIN_AUTH_COOKIE_NAME,
                           new_cookie, max_age=EXPIRED_TIME)
        if save_user:
            user.save()

    def register_routes(self, app: Quart):

        @app.route(
            urljoin(self.setting['public_basepath'], 'login/'),
            methods=['GET', 'POST'])
        async def yobot_login():
            prefix = self.setting['preffix_string'] if self.setting['preffix_on'] else ''
            if request.method == "POST":
                form = await request.form

            def get_params(k: str) -> str:
                return request.args.get(k) \
                    if request.method == "GET" \
                    else (form and k in form and form[k])

            try:
                qqid = get_params('qqid')
                key = get_params('key')
                pwd = get_params('pwd')
                callback_page = get_params('callback') or url_for('yobot_user')
                auth_cookie = request.cookies.get(LOGIN_AUTH_COOKIE_NAME)

                if not qqid and not auth_cookie:
                    # 普通登录
                    return await render_template(
                        'login.html',
                        advice=f'请私聊机器人“{self._get_prefix()}登录”获取登录地址 ',
                        prefix=self._get_prefix()
                    )

                key_failure = None
                if qqid:
                    user = User.get_or_none(User.qqid == qqid)
                    if key:
                        try:
                            self._check_key(user, key)
                        except ExceptionWithAdvice as e:
                            if auth_cookie:
                                qqid = None
                                key_failure = e
                            else:
                                raise e from e
                    if pwd:
                        self._check_pwd(user, pwd)

                if auth_cookie and not qqid:
                    # 可能用于用cookie寻回session

                    if 'yobot_user' in session:
                        # 会话未过期
                        return redirect(callback_page)
                    try:
                        user = self._recall_from_cookie(auth_cookie)
                    except ExceptionWithAdvice as e:
                        if key_failure is not None:
                            raise key_failure
                        else:
                            raise e from e
                    self._set_auth_info(user)
                    return redirect(callback_page)

                if not key and not pwd:
                    raise ExceptionWithAdvice("无效的登录地址", "请检查登录地址是否完整")

                res = await make_response(redirect(callback_page))
                self._set_auth_info(user, res, save_user=False)
                user.login_code_available = False
                user.save()
                return res

            except ExceptionWithAdvice as e:
                return await render_template(
                    'login.html',
                    reason=e.reason,
                    advice=e.advice or f'请私聊机器人“{self._get_prefix()}登录”获取登录地址 ',
                    prefix=prefix
                )

        @app.route(
            urljoin(self.setting['public_basepath'], 'logout/'),
            methods=['GET', 'POST'])
        async def yobot_logout():
            session.clear()
            res = await make_response(redirect(url_for('yobot_login')))
            res.delete_cookie(LOGIN_AUTH_COOKIE_NAME)
            return res

        @app.route(
            urljoin(self.setting['public_basepath'], 'user/'),
            endpoint='yobot_user',
            methods=['GET'])
        @app.route(
            urljoin(self.setting['public_basepath'], 'admin/'),
            endpoint='yobot_admin',
            methods=['GET'])
        async def yobot_user():
            if 'yobot_user' not in session:
                return redirect(url_for('yobot_login', callback=request.path))
            return await render_template(
                'user.html',
                user=User.get_by_id(session['yobot_user']),
            )

        @app.route(
            urljoin(self.setting['public_basepath'], 'user/<int:qqid>/'),
            methods=['GET'])
        async def yobot_user_info(qqid):
            if 'yobot_user' not in session:
                return redirect(url_for('yobot_login', callback=request.path))
            if session['yobot_user'] == qqid:
                visited_user_info = User.get_by_id(session['yobot_user'])
            else:
                visited_user = User.get_or_none(User.qqid == qqid)
                if visited_user is None:
                    return '没有此用户', 404
                visited_user_info = visited_user
            return await render_template(
                'user-info.html',
                user=visited_user_info,
                visitor=User.get_by_id(session['yobot_user']),
            )

        @app.route(
            urljoin(self.setting['public_basepath'],
                    'user/<int:qqid>/nickname/'),
            methods=['PUT'])
        async def yobot_user_info_nickname(qqid):
            if 'yobot_user' not in session:
                return jsonify(code=10, message='未登录')
            user = User.get_by_id(session['yobot_user'])
            if user.qqid != qqid and user.authority_group >= 100:
                return jsonify(code=11, message='权限不足')
            user_data = User.get_or_none(User.qqid == qqid)
            if user_data is None:
                return jsonify(code=20, message='用户不存在')
            new_setting = await request.get_json()
            if new_setting is None:
                return jsonify(code=30, message='消息体格式错误')
            new_nickname = new_setting.get('nickname')
            if new_nickname is None:
                return jsonify(code=32, message='消息体内容错误')
            user_data.nickname = new_nickname
            user_data.save()
            return jsonify(code=0, message='success')

        @app.route(
            urljoin(self.setting['public_basepath'], 'user/reset-password/'),
            methods=['GET', 'POST'])
        async def yobot_reset_pwd():
            try:
                if 'yobot_user' not in session:
                    return redirect(url_for('yobot_login', callback=request.path))
                if request.method == "GET":
                    return await render_template('password.html')

                qq = session['yobot_user']
                user = User.get_or_none(User.qqid == qq)
                if user is None:
                    raise Exception("请先加公会")
                form = await request.form
                pwd = form["pwd"]
                # self._validate_pwd(pwd)
                user.password = _add_salt_and_hash(pwd, user.salt)
                user.privacy = 0
                user.save()
                return await render_template(
                    'password.html',
                    success="密码设置成功",
                )
            except Exception as e:
                return await render_template(
                    'password.html',
                    error=str(e)
                )
