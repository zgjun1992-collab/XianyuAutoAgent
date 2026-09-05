import time
import os
import re
import sys
import json
import mimetypes

import requests
from loguru import logger
from utils.xianyu_utils import generate_sign


class XianyuApis:
    def __init__(self, interactive=True):
        self.interactive = interactive
        self.url = 'https://h5api.m.goofish.com/h5/mtop.taobao.idlemessage.pc.login.token/1.0/'
        self.session = requests.Session()
        self.session.headers.update({
            'accept': 'application/json',
            'accept-language': 'zh-CN,zh;q=0.9',
            'cache-control': 'no-cache',
            'origin': 'https://www.goofish.com',
            'pragma': 'no-cache',
            'priority': 'u=1, i',
            'referer': 'https://www.goofish.com/',
            'sec-ch-ua': '"Not(A:Brand";v="99", "Google Chrome";v="133", "Chromium";v="133"',
            'sec-ch-ua-mobile': '?0',
            'sec-ch-ua-platform': '"Windows"',
            'sec-fetch-dest': 'empty',
            'sec-fetch-mode': 'cors',
            'sec-fetch-site': 'same-site',
            'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36',
        })

    def upload_media(self, media_path):
        """Upload a local image to Xianyu's chat media service."""
        media_path = os.path.abspath(str(media_path or ""))
        if not os.path.isfile(media_path):
            raise FileNotFoundError("套餐图片文件不存在")
        mime_type = mimetypes.guess_type(media_path)[0] or "image/png"
        if mime_type not in {"image/png", "image/jpeg", "image/webp"}:
            raise ValueError("套餐图片仅支持 PNG、JPG、JPEG 或 WebP")
        headers = {
            "accept": "*/*",
            "origin": "https://www.goofish.com",
            "referer": "https://www.goofish.com/",
            "user-agent": self.session.headers.get("user-agent", "Mozilla/5.0"),
        }
        params = {"floderId": "0", "appkey": "xy_chat", "_input_charset": "utf-8"}
        with open(media_path, "rb") as handle:
            files = {"file": (os.path.basename(media_path), handle, mime_type)}
            response = self.session.post(
                "https://stream-upload.goofish.com/api/upload.api",
                headers=headers,
                params=params,
                files=files,
                timeout=45,
            )
        response.raise_for_status()
        payload = response.json()
        image_object = payload.get("object") if isinstance(payload, dict) else None
        if not isinstance(image_object, dict) or not image_object.get("url"):
            raise RuntimeError("闲鱼图片上传失败，请检查登录状态后重试")
        return payload
        
    def clear_duplicate_cookies(self):
        """清理重复的cookies"""
        # 创建一个新的CookieJar
        new_jar = requests.cookies.RequestsCookieJar()
        
        # 记录已经添加过的cookie名称
        added_cookies = set()
        
        # 按照cookies列表的逆序遍历（最新的通常在后面）
        cookie_list = list(self.session.cookies)
        cookie_list.reverse()
        
        for cookie in cookie_list:
            # 如果这个cookie名称还没有添加过，就添加到新jar中
            if cookie.name not in added_cookies:
                new_jar.set_cookie(cookie)
                added_cookies.add(cookie.name)
                
        # 替换session的cookies
        self.session.cookies = new_jar
        
        # 更新完cookies后，更新.env文件
        self.update_env_cookies()
        
    def update_env_cookies(self):
        """更新.env文件中的COOKIES_STR"""
        try:
            # 获取当前cookies的字符串形式
            cookie_str = '; '.join([f"{cookie.name}={cookie.value}" for cookie in self.session.cookies])
            
            # 读取.env文件
            env_path = os.path.join(os.getcwd(), '.env')
            if not os.path.exists(env_path):
                logger.warning(".env文件不存在，无法更新COOKIES_STR")
                return
                
            with open(env_path, 'r', encoding='utf-8') as f:
                env_content = f.read()
                
            # 使用正则表达式替换COOKIES_STR的值
            if 'COOKIES_STR=' in env_content:
                new_env_content = re.sub(
                    r'COOKIES_STR=.*', 
                    f'COOKIES_STR={cookie_str}',
                    env_content
                )
                
                # 写回.env文件
                with open(env_path, 'w', encoding='utf-8') as f:
                    f.write(new_env_content)
                    
                logger.debug("已更新.env文件中的COOKIES_STR")
            else:
                logger.warning(".env文件中未找到COOKIES_STR配置项")
        except Exception as e:
            logger.warning(f"更新.env文件失败: {str(e)}")
        
    def hasLogin(self, retry_count=0):
        """调用hasLogin.do接口进行登录状态检查"""
        if retry_count >= 2:
            logger.error("Login检查失败，重试次数过多")
            return False
            
        try:
            url = 'https://passport.goofish.com/newlogin/hasLogin.do'
            params = {
                'appName': 'xianyu',
                'fromSite': '77'
            }
            data = {
                'hid': self.session.cookies.get('unb', ''),
                'ltl': 'true',
                'appName': 'xianyu',
                'appEntrance': 'web',
                '_csrf_token': self.session.cookies.get('XSRF-TOKEN', ''),
                'umidToken': '',
                'hsiz': self.session.cookies.get('cookie2', ''),
                'bizParams': 'taobaoBizLoginFrom=web',
                'mainPage': 'false',
                'isMobile': 'false',
                'lang': 'zh_CN',
                'returnUrl': '',
                'fromSite': '77',
                'isIframe': 'true',
                'documentReferer': 'https://www.goofish.com/',
                'defaultView': 'hasLogin',
                'umidTag': 'SERVER',
                'deviceId': self.session.cookies.get('cna', '')
            }
            
            response = self.session.post(url, params=params, data=data)
            res_json = response.json()
            
            if res_json.get('content', {}).get('success'):
                logger.debug("Login成功")
                # 清理和更新cookies
                self.clear_duplicate_cookies()
                return True
            else:
                logger.warning(f"Login失败: {res_json}")
                time.sleep(0.5)
                return self.hasLogin(retry_count + 1)
                
        except Exception as e:
            logger.error(f"Login请求异常: {str(e)}")
            time.sleep(0.5)
            return self.hasLogin(retry_count + 1)

    def get_token(self, device_id, retry_count=0):
        if retry_count >= 2:  # 最多重试3次
            logger.warning("获取token失败，尝试重新登陆")
            # 尝试通过hasLogin重新登录
            if self.hasLogin():
                logger.info("重新登录成功，重新尝试获取token")
                return self.get_token(device_id, 0)  # 重置重试次数
            else:
                logger.error("重新登录失败，Cookie已失效")
                message = "Cookie已失效，请更新Cookie后重新启动客服"
                logger.error(f"🔴 {message}")
                if not self.interactive:
                    raise RuntimeError(message)
                sys.exit(1)

        params = {
            'jsv': '2.7.2',
            'appKey': '34839810',
            't': str(int(time.time()) * 1000),
            'sign': '',
            'v': '1.0',
            'type': 'originaljson',
            'accountSite': 'xianyu',
            'dataType': 'json',
            'timeout': '20000',
            'api': 'mtop.taobao.idlemessage.pc.login.token',
            'sessionOption': 'AutoLoginOnly',
            'spm_cnt': 'a21ybx.im.0.0',
            "spm_pre": "a21ybx.item.want.1.14ad3da6ALVq3n",
            "log_id": "14ad3da6ALVq3n"
        }
        data_val = '{"appKey":"444e9908a51d1cb236a27862abc769c9","deviceId":"' + device_id + '"}'
        data = {
            'data': data_val,
        }
        headers = {
            "Host": "h5api.m.goofish.com",
            "sec-ch-ua-platform": "\"Windows\"",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
            "accept": "application/json",
            "sec-ch-ua": "\"Chromium\";v=\"146\", \"Not-A.Brand\";v=\"24\", \"Google Chrome\";v=\"146\"",
            "content-type": "application/x-www-form-urlencoded",
            "sec-ch-ua-mobile": "?0",
            "origin": "https://www.goofish.com",
            "sec-fetch-site": "same-site",
            "sec-fetch-mode": "cors",
            "sec-fetch-dest": "empty",
            "referer": "https://www.goofish.com/",
            "accept-language": "en,zh-CN;q=0.9,zh;q=0.8,zh-TW;q=0.7,ja;q=0.6",
            "priority": "u=1, i"
        }
        # 简单获取token，信任cookies已清理干净
        token = self.session.cookies.get('_m_h5_tk', '').split('_')[0]
        
        sign = generate_sign(params['t'], token, data_val)
        params['sign'] = sign
        
        try:
            response = self.session.post('https://h5api.m.goofish.com/h5/mtop.taobao.idlemessage.pc.login.token/1.0/', headers=headers, params=params, data=data)
            res_json = response.json()
            
            if isinstance(res_json, dict):
                ret_value = res_json.get('ret', [])
                # 检查ret是否包含成功信息
                if not any('SUCCESS::调用成功' in ret for ret in ret_value):
                    # 检测风控/限流错误
                    error_msg = str(ret_value)
                    if 'RGV587_ERROR' in error_msg or '被挤爆啦' in error_msg:
                        logger.error(f"❌ 触发风控: {ret_value}")
                        logger.error("🔴 系统目前无法自动解决，请进入闲鱼网页版-点击消息-过滑块-复制最新的Cookie")
                        if not self.interactive:
                            raise RuntimeError("闲鱼触发风控，请在网页版完成验证并更新Cookie")
                        
                        # 获取用户输入的新Cookie
                        print("\n" + "="*50)
                        new_cookie_str = input("请输入新的Cookie字符串 (复制浏览器中的完整cookie，直接回车则退出程序): ").strip()
                        print("="*50 + "\n")
                        
                        if new_cookie_str:
                            try:
                                # 解析cookie字符串并更新session
                                from http.cookies import SimpleCookie
                                cookie = SimpleCookie()
                                cookie.load(new_cookie_str)
                                
                                # 清空旧cookie并设置新cookie
                                self.session.cookies.clear()
                                for key, morsel in cookie.items():
                                    self.session.cookies.set(key, morsel.value, domain='.goofish.com')
                                
                                logger.success("✅ Cookie已更新，正在尝试重连...")
                                # 同步更新到.env文件
                                self.update_env_cookies()
                                
                                # 立即重试
                                return self.get_token(device_id, 0)
                            except Exception as e:
                                logger.error(f"Cookie解析失败: {e}")
                                sys.exit(1)
                        else:
                            logger.info("用户取消输入，程序退出")
                            sys.exit(1)

                    logger.warning(f"Token API调用失败，错误信息: {ret_value}")
                    # 处理响应中的Set-Cookie
                    if 'Set-Cookie' in response.headers:
                        logger.debug("检测到Set-Cookie，更新cookie")  # 降级为DEBUG并简化
                        self.clear_duplicate_cookies()
                    time.sleep(0.5)
                    return self.get_token(device_id, retry_count + 1)
                else:
                    logger.info("Token获取成功")
                    return res_json
            else:
                logger.error(f"Token API返回格式异常: {res_json}")
                return self.get_token(device_id, retry_count + 1)
                
        except Exception as e:
            logger.error(f"Token API请求异常: {str(e)}")
            time.sleep(0.5)
            return self.get_token(device_id, retry_count + 1)

    def get_item_info(self, item_id, retry_count=0):
        """获取商品信息，自动处理token失效的情况"""
        if retry_count >= 3:  # 最多重试3次
            logger.error("获取商品信息失败，重试次数过多")
            return {"error": "获取商品信息失败，重试次数过多"}
            
        params = {
            'jsv': '2.7.2',
            'appKey': '34839810',
            't': str(int(time.time()) * 1000),
            'sign': '',
            'v': '1.0',
            'type': 'originaljson',
            'accountSite': 'xianyu',
            'dataType': 'json',
            'timeout': '20000',
            'api': 'mtop.taobao.idle.pc.detail',
            'sessionOption': 'AutoLoginOnly',
            'spm_cnt': 'a21ybx.im.0.0',
        }
        
        data_val = '{"itemId":"' + item_id + '"}'
        data = {
            'data': data_val,
        }
        
        # 简单获取token，信任cookies已清理干净
        token = self.session.cookies.get('_m_h5_tk', '').split('_')[0]
        
        sign = generate_sign(params['t'], token, data_val)
        params['sign'] = sign
        
        try:
            response = self.session.post(
                'https://h5api.m.goofish.com/h5/mtop.taobao.idle.pc.detail/1.0/', 
                params=params, 
                data=data
            )
            
            res_json = response.json()
            # 检查返回状态
            if isinstance(res_json, dict):
                ret_value = res_json.get('ret', [])
                # 检查ret是否包含成功信息
                if not any('SUCCESS::调用成功' in ret for ret in ret_value):
                    logger.warning(f"商品信息API调用失败，错误信息: {ret_value}")
                    # 处理响应中的Set-Cookie
                    if 'Set-Cookie' in response.headers:
                        logger.debug("检测到Set-Cookie，更新cookie")
                        self.clear_duplicate_cookies()
                    time.sleep(0.5)
                    return self.get_item_info(item_id, retry_count + 1)
                else:
                    logger.debug(f"商品信息获取成功: {item_id}")
                    return res_json
            else:
                logger.error(f"商品信息API返回格式异常: {res_json}")
                return self.get_item_info(item_id, retry_count + 1)
                
        except Exception as e:
            logger.error(f"商品信息API请求异常: {str(e)}")
            time.sleep(0.5)
            return self.get_item_info(item_id, retry_count + 1)

    def get_user_items(self, user_id, page_number=1, page_size=20, page_state=None):
        """Read one page of the logged-in seller's public item cards."""
        payload = {
            "needGroupInfo": page_number == 1,
            "pageNumber": int(page_number),
            "userId": str(user_id),
            "pageSize": int(page_size),
        }
        if page_state:
            for key in (
                "groupName", "groupId", "defaultGroup", "groupSortId",
                "filterPanelGroupId", "nextPageModel", "nextPageNum",
            ):
                if page_state.get(key) is not None:
                    payload[key] = page_state[key]
        data_val = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        request_time = str(int(time.time() * 1000))
        token = self.session.cookies.get("_m_h5_tk", "").split("_")[0]
        params = {
            "jsv": "2.7.2",
            "appKey": "34839810",
            "t": request_time,
            "sign": generate_sign(request_time, token, data_val),
            "v": "1.0",
            "type": "originaljson",
            "accountSite": "xianyu",
            "dataType": "json",
            "timeout": "20000",
            "api": "mtop.idle.web.xyh.item.list",
            "sessionOption": "AutoLoginOnly",
            "spm_cnt": "a21ybx.personal.0.0",
        }
        response = self.session.post(
            "https://h5api.m.goofish.com/h5/mtop.idle.web.xyh.item.list/1.0/",
            params=params,
            data={"data": data_val},
            timeout=25,
        )
        result = response.json()
        ret = result.get("ret", []) if isinstance(result, dict) else []
        if not any("SUCCESS::调用成功" in value for value in ret):
            raise RuntimeError("读取闲鱼商品列表失败：" + "；".join(ret or ["返回格式异常"]))
        return result

    def get_all_user_items(self, user_id, page_size=20, max_pages=50):
        """Aggregate all cards and let the caller filter itemStatus=0 (on sale)."""
        cards = []
        page_state = {}
        # Consumers may only treat an absent item as offline after the complete
        # paginated listing has been read.  Keep this explicit so a transient
        # page-limit/network condition can never take an active product down.
        self.last_item_list_complete = False
        for page_number in range(1, max_pages + 1):
            result = self.get_user_items(user_id, page_number, page_size, page_state)
            data = result.get("data") or {}
            cards.extend(data.get("cardList") or [])
            if not data.get("nextPage"):
                self.last_item_list_complete = True
                break
            page_state.update({
                "nextPageModel": data.get("nextPageModel"),
                "nextPageNum": data.get("nextPageNum"),
            })
            if page_number == 1:
                groups = data.get("itemGroupList") or []
                default_group = next((group for group in groups if group.get("defaultGroup")), None)
                if default_group:
                    page_state.update({
                        "groupName": default_group.get("groupName"),
                        "groupId": default_group.get("groupId"),
                        "defaultGroup": True,
                    })
            time.sleep(0.15)
        return cards
