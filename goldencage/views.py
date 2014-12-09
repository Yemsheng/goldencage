# encoding=utf-8

from django.http import HttpResponseForbidden
from django.http import HttpResponse
from django.http import HttpResponseNotAllowed
from django.conf import settings
from django.views.decorators.csrf import csrf_exempt
from django.core.cache import cache

from Crypto.PublicKey import RSA
from Crypto.Signature import PKCS1_v1_5
from Crypto.Hash import SHA

import base64
import hashlib
import urllib
import requests
import simplejson as json
from random import Random
import time
# import xmltodict
from xml.dom import minidom

from goldencage.models import AppWallLog
from goldencage.models import Charge
from goldencage import config
from goldencage.models import Coupon
from goldencage.models import ChargePlan


from wechat.official import WxApplication, WxTextResponse, WxResponse

import logging
log = logging.getLogger(__name__)


def rsp(data=None):
    if data is None:
        data = {}
    ret = {'errcode': 0, 'errmsg': 'ok', 'data': data}
    ret = json.dumps(ret)
    return HttpResponse(ret)


def error_rsp(code, msg, data=None):
    if data is None:
        data = {}
    ret = {'errcode': code, 'errmsg': msg, 'data': data}
    ret = json.dumps(ret)
    return HttpResponse({'errcode': code, 'errmsg': msg, 'data': data})


waps_ips = ['219.234.85.238', '219.234.85.223',
            '219.234.85.211', '219.234.85.231',
            '127.0.0.1']


def waps_callback(request):
    ip = request.META.get("REMOTE_ADDR", None)

    if ip not in waps_ips and not request.GET.get('debug', None):
        return HttpResponseNotAllowed("incorrect IP address")
    wapslog = {}
    for key in request.GET.keys():
        wapslog[key] = request.GET[key]
    if AppWallLog.log(wapslog, provider='waps'):
        return HttpResponse(json.dumps(
            {"message": u"成功接收", "success": True}))
    else:
        return HttpResponse(json.dumps(
            {"message": u"无效数据", "success": False}))


def youmi_callback_adr(request):
    sign = request.GET.get('sig')
    if not sign:
        return HttpResponseForbidden("miss param 'sign'")

    keys = ['order', 'app', 'user', 'chn', 'ad', 'points']
    vals = [request.GET.get(k, '').encode('utf8').decode('utf8') for k in keys]
    vals.insert(0, settings.YOUMI_CALLBACK_SECRET_ADR)
    token = u'||'.join(vals)
    md5 = hashlib.md5()
    md5.update(token.encode('utf-8'))
    md5 = md5.hexdigest()
    _sign = md5[12:20]

    if sign != _sign:
        return HttpResponseForbidden("signature error")
    youmilog = {}
    for key in keys:
        youmilog[key] = request.GET[key]
    if AppWallLog.log(youmilog, provider='youmi_adr'):
        return HttpResponse('OK')
    else:
        return HttpResponseForbidden('already exist')

    return HttpResponseForbidden("Signature verification fail")


def youmi_callback_ios(request):
    sign = request.GET.get('sign')
    if not sign:
        return HttpResponseForbidden("miss param 'sign'")

    keys = request.GET.keys()
    keys.sort()

    src = ''.join(['%s=%s' %
                   (k, request.GET.get(k).encode('utf-8').decode('utf-8'))
                   for k in keys if k != 'sign'])
    src += settings.YOUMI_CALLBACK_SECRET
    md5 = hashlib.md5()
    md5.update(src.encode('utf-8'))
    _sign = md5.hexdigest()

    if sign != _sign:
        return HttpResponseForbidden("signature error")

    youmilog = {}
    for key in keys:
        youmilog[key] = request.GET[key]
    if AppWallLog.log(youmilog, provider='youmi_ios'):
        return HttpResponse('OK')
    else:
        return HttpResponseForbidden('already exist')

    return HttpResponseForbidden("Signature verification fail")


def dianjoy_callback_adr(request):
    token = request.GET.get('token')
    time_stamp = request.GET.get('time_stamp')
    md5 = hashlib.md5()
    md5.update(time_stamp + settings.GOLDENCAGE_DIANJOY_ANDROID_SECRET)
    sign = md5.hexdigest()
    if sign != token:
        return HttpResponseForbidden('token error')
    log = {}
    for key in request.GET.keys():
        log[key] = request.GET[key]
    if AppWallLog.log(log, provider='dianjoy_adr'):
        return HttpResponse('OK')
    else:
        return HttpResponse('OK, But duplicate item')


def appwall_callback(request, provider):
    return {'waps': waps_callback,
            'youmi_ios': youmi_callback_ios,
            'youmi_adr': youmi_callback_adr,
            'dianjoy_adr': dianjoy_callback_adr,
            }[provider](request)

alipay_public_key = config.ALIPAY_PUBLIC_KEY


# 支付宝回调 ########

def verify_notify_id(notify_id):
    # 检查是否合法的notify_id, 检测该id是否已被成功处理过。

    url = 'https://mapi.alipay.com/gateway.do'
    params = {'service': 'notify_verify',
              'partner': settings.ALIPAY_PID,
              'notify_id': notify_id}
    log.info('start verify notify_id %s' % notify_id)
    try:
        rsp = requests.get(url, params=params, timeout=5)
    except:
        log.error('timeout verify notify_id %s' % notify_id)
        return False
    log.info('finish verify notifi_id %s' % notify_id)
    return rsp.status_code == 200 and rsp.text == 'true'


def verify_alipay_signature(sign_type, sign, params):
    if sign_type == 'RSA':
        return rsa_verify(params, sign)
    else:
        return True


def filter_para(paras):
    """过滤空值和签名"""
    for k, v in paras.items():
        if not v or k in ['sign', 'sign_type']:
            paras.pop(k)
    return paras


def create_link_string(paras, sort, encode):
    """对参数排序并拼接成query string的形式"""
    if sort:
        paras = sorted(paras.items(), key=lambda d: d[0])
    if encode:
        return urllib.urlencode(paras)
    else:
        if not isinstance(paras, list):
            paras = list(paras.items())
        ps = ''
        for p in paras:
            if ps:
                ps = '%s&%s=%s' % (ps, p[0], p[1])
            else:
                ps = '%s=%s' % (p[0], p[1])
        return ps


def rsa_verify(paras, sign):
    """对签名做rsa验证"""
    log.debug('init paras = %s' % paras)
    pub_key = RSA.importKey(config.ALIPAY_PUBLIC_KEY)
    paras = filter_para(paras)
    paras = create_link_string(paras, True, False)
    log.debug('type(paras) = %s paras = %s' % (type(paras), paras))
    verifier = PKCS1_v1_5.new(pub_key)
    data = SHA.new(paras.encode('utf-8'))
    return verifier.verify(data, base64.b64decode(sign))


@csrf_exempt
def alipay_callback(request):
    # 支付宝支付回调，先检查签名是否正确，再检查是否来自支付宝的请求。
    # 有效的回调，将更新用户的资产。
    keys = request.REQUEST.keys()
    data = {}
    for key in keys:
        data[key] = request.REQUEST[key]
    notify_id = data['notify_id']
    sign_type = data['sign_type']
    sign = data['sign']
    order_id = data['out_trade_no']

    log.info(u'alipay callback, order_id: %s , data: %s' % (order_id, data))

    nid = cache.get('ali_nid_' + hashlib.sha1(notify_id).hexdigest())
    if nid:
        log.info('duplicated notify, drop it')
        return HttpResponse('error')

    if verify_notify_id(notify_id) \
            and verify_alipay_signature(sign_type, sign, data) \
            and Charge.recharge(data, provider='alipay'):
        cache.set('ali_nid_' + hashlib.sha1(notify_id).hexdigest(),
                  order_id, 90000)  # notify_id 保存25小时。
        log.info('ali callback success')
        return HttpResponse('success')
    log.info('not a valid callback, ignore')
    return HttpResponse('error')


def rsa_sign(para_str):
    """对请求参数做rsa签名"""
    para_str = para_str.encode('utf-8')
    key = RSA.importKey(settings.ALIPAY_PRIVATE_KEY)
    h = SHA.new(para_str)
    signer = PKCS1_v1_5.new(key)
    return base64.b64encode(signer.sign(h))


@csrf_exempt
def alipay_sign(request):
    if request.method != 'POST':
        logging.error('equest.method != "POST"')
        return error_rsp(5099, 'error')

    log.debug('request.POST = %s' % request.POST)
    words = request.POST.get('words')
    if not words:
        logging.error('if not words')
        return error_rsp(5099, 'error')

    sign_type = request.POST.get('sign_type')
    if not sign_type:
        sign_type = 'RSA'

    if sign_type == 'RSA':
        en_str = rsa_sign(words)
    else:
        en_str = ''

    data = {'en_words': en_str}
    return rsp(data)


class WxEmptyResponse(WxResponse):

    def as_xml(self):
        return ''


class ChatView(WxApplication):
    SECRET_TOKEN = getattr(settings, 'GOLDENCAGE_WECHAT_TOKEN', '')
    BALANCE_UNIT_NAME = getattr(settings, 'GOLDENCAGE_BALANCE_UNIT_NAME',
                                u'金币')
    SUCCESS_MESSAGE_TEMPLATE = getattr(
        settings, 'GOLDENCAGE_COUPONE_SUCCESS_MESSAGE_TEMPLATE',
        u'您已获得了%d%s')

    def on_text(self, text):
        content = text.Content.lower()
        coupons = Coupon.objects.filter(disable=False, exchange_style='wechat')
        for cp in coupons:
            if content.startswith(cp.key):
                content = content.replace(cp.key, '').strip()
                result = cp.validate(content)
                if result:
                    return WxTextResponse(
                        self.SUCCESS_MESSAGE_TEMPLATE %
                        (cp.cost, self.BALANCE_UNIT_NAME), text)
                else:
                    return WxTextResponse(u'无效的兑换码,或已被兑换过。',
                                          text)
        return WxEmptyResponse(text)


@csrf_exempt
def wechat(request):
    """只处理文本，并且只处理一个命令。
    """
    app = ChatView()
    if request.method == 'GET':
        # 用于校验访问权限, 直接返回一字符串即可。
        rsp = app.process(request.GET)
        return HttpResponse(rsp)
    elif request.method == 'POST':
        rsp = app.process(request.GET, request.body)
        if not rsp:
            return HttpResponse('')
        return HttpResponse(rsp)


# 微信支付
WECHATPAY_ACCESS_TOKEN_URL = 'https://api.weixin.qq.com/cgi-bin/token'


def wechat_pay_get_access_token():
    params = {
        'grant_type': 'client_credential',
        'appid': settings.WECHATPAY_APPID,
        'secret': settings.WECHATPAY_SECRET
    }
    rsp = requests.get(WECHATPAY_ACCESS_TOKEN_URL, params=params)
    content = json.loads(rsp.content)
    errcode = content.get('errcode')
    if errcode:
        log.error(errcode)
        log.error(content['errmsg'])
        return {}
    else:
        return content


@csrf_exempt
def wechat_pay_gen_package(request):
    if request.method != 'POST':
        logging.error('equest.method != "POST"')
        return error_rsp(5099, 'error')

    log.debug(u'request.body = %s' % request.body)
    log.debug(u'body type = %s' % type(request.body))
    body = json.loads(request.body)
    package = body.get('package')
    if not package:
        return error_rsp(5099, 'error')
    package = _wechatpay_gen_package(package)
    log.debug(u'package = %s' % package)

    noncestr = random_str(13)
    timestamp = '%.f' % time.time()
    traceid = body.get('traceid', '')
    sha_param = {
        'appid': settings.WECHATPAY_APPID,
        'appkey': settings.WECHATPAY_APPKEY,
        'noncestr': noncestr,
        'package': package,
        'timestamp': timestamp,
        'traceid': traceid}
    app_signature = _wechatpay_app_signature(sha_param)
    log.debug(u'app_signature = %s' % app_signature)

    data = {'package': package}
    data['appid'] = settings.WECHATPAY_APPID
    data['noncestr'] = noncestr
    data['traceid'] = traceid
    data['timestamp'] = timestamp
    data['sign_method'] = 'sha1'
    data['app_signature'] = app_signature
    data['partnerid'] = settings.WECHATPAY_PARTNERID
    data['appkey'] = settings.WECHATPAY_APPKEY

    log.debug(u'rsp data = %s' % data)

    return rsp(data)


def convert_params_to_str_in_order(params):
    param_list = sorted(params.iteritems(), key=lambda d: d[0])
    log.debug(u'param_list = %s' % param_list)
    tmp_str = u''
    for val in param_list:
        if tmp_str:
            tmp_str = tmp_str + '&%s=%s' % (val[0], val[1])
        else:
            tmp_str = '%s=%s' % (val[0], val[1])
    return tmp_str.encode('utf-8')


def convert_params_to_str_in_order_urlcode(params):
    param_list = sorted(params.iteritems(), key=lambda d: d[0])
    log.debug('param_list = %s' % param_list)
    tmp_str = u''
    for val in param_list:
        vall = u'%s' % val[1]
        vall = vall.encode('utf-8')
        log.debug('vall = %s' % vall)
        vall = urllib.quote(vall)
        if tmp_str:
            append_str = u'&%s=%s' % (val[0], vall)
            tmp_str = tmp_str + append_str
        else:
            tmp_str = u'%s=%s' % (val[0], vall)
    return tmp_str.encode('utf-8')


def _wechatpay_gen_package(
        package=None, body=None, out_trade_no=None,
        total_fee=None, ip=None):
    if not package:
        package = {}
    package['bank_type'] = 'WX'
    package['body'] = body or ''
    package['attach'] = ''
    package['partner'] = settings.WECHATPAY_PARTNERID
    package['out_trade_no'] = out_trade_no or ''
    package['total_fee'] = total_fee or ''
    package['fee_type'] = 1
    package['notify_url'] = settings.WECHATPAY_NOTIFY_URL
    package['spbill_create_ip'] = ip or ''
    package['time_start'] = ''
    package['time_expire'] = ''
    package['transport_fee'] = ''
    package['product_fee'] = ''
    package['goods_tag'] = ''
    package['input_charset'] = 'GBK'

    string1 = convert_params_to_str_in_order(package)
    stringSignTemp = string1 + '&key=%s' % settings.WECHATPAY_PARTNERKEY
    log.debug(u'stringSignTemp = %s' % stringSignTemp)
    md5 = hashlib.md5()
    md5.update(stringSignTemp)
    sign_str = md5.hexdigest().upper()
    log.debug(u'sign = %s' % sign_str)
    string1 = convert_params_to_str_in_order_urlcode(package)
    package = string1 + '&sign=%s' % sign_str
    return package


def _wechatpay_app_signature(params):
    params_str = convert_params_to_str_in_order(params)
    sign = hashlib.sha1(params_str).hexdigest()
    return sign


def random_str(randomlength=8):
    str_ = ''
    chars = 'abcdefghijklmnopqrstuvwxyz0123456789'
    length = len(chars) - 1
    random = Random()
    for i in range(randomlength):
        str_ += chars[random.randint(0, length)]
    return str_


def wechatpay_prepayid_params(planid, out_trade_no, client_ip, traceid):
    plan = ChargePlan.objects.get(pk=int(planid))
    package = _wechatpay_gen_package(
        package=None, body=plan.name, out_trade_no=out_trade_no,
        total_fee=plan.value, ip=client_ip)
    noncestr = random_str(13)
    timestamp = '%.f' % time.time()
    sha_param = {
        'appid': settings.WECHATPAY_APPID,
        'appkey': settings.WECHATPAY_APPKEY,
        'noncestr': noncestr,
        'package': package,
        'timestamp': timestamp,
        'traceid': traceid}
    app_signature = _wechatpay_app_signature(sha_param)
    log.debug(u'app_signature = %s' % app_signature)

    data = {'package': package}
    data['appid'] = settings.WECHATPAY_APPID
    data['noncestr'] = noncestr
    data['traceid'] = traceid
    data['timestamp'] = timestamp
    data['sign_method'] = 'sha1'
    data['app_signature'] = app_signature

    log.debug(u'rsp data = %s' % data)

    return data


def wechatpay_sign_result(noncestr, prepayid, timestamp):
    """ 要不要去掉 最后一个 &
    """
    raw_str = (
        u'appid=%s&appkey=%s&noncestr=%s&package=Sign=WXPay&'
        u'partnerid=%s&prepayid=%s&timestamp=%s&'
        % (
            settings.WECHATPAY_APPID,
            settings.WECHATPAY_APPKEY,
            noncestr,
            settings.WECHATPAY_PARTNERID,
            prepayid,
            str(timestamp)
        )
    )
    signResult = hashlib.sha1(raw_str).hexdigest()
    return signResult


def wechatpay_get_info(
        access_token, planid,
        out_trade_no, client_ip, traceid):

    """
    提供外部调用的接口
    traceid 商家对用户的唯一标识,如果用微信 SSO,此处建议填写 授权用户的 openid
    """

    data = wechatpay_prepayid_params(
        planid, out_trade_no, client_ip, traceid)

    params = {
        'access_token': access_token
    }
    url = 'https://api.weixin.qq.com/pay/genprepay'
    headers = {
        'content-type': 'application/json'
    }
    post_data = json.dumps(data)
    rsp = requests.post(url, params=params, data=post_data, headers=headers)
    content = json.loads(rsp.content)
    if content['errcode'] != 0:
        log.error(content['errmsg'])
        return None
    signResult = wechatpay_sign_result(
        data['noncestr'],
        content['prepayid'],
        data['timestamp'])

    wechatpay_data = {}
    wechatpay_data['partnerid'] = settings.WECHATPAY_PARTNERID
    wechatpay_data['prapayid'] = content['prepayid']
    wechatpay_data['package'] = data['package']
    wechatpay_data['noncestr'] = data['noncestr']
    wechatpay_data['timestamp'] = data['timestamp']
    wechatpay_data['sign'] = signResult

    return wechatpay_data


@csrf_exempt
def wechat_pay_notify(request):
    if request.method != 'POST':
        logging.error('equest.method != "POST"')
        return HttpResponse('fail')
    if not _wechatpay_verify_notify(request.GET):
        return HttpResponse('fail')

    log.debug(u'type trade_state = %s' % type(request.GET['trade_state']))

    notify_id = request.GET['notify_id']
    order_id = request.GET['out_trade_no']
    log.info(u'request.GET = %s' % request.GET)
    log.info(u'wechatpay callback, order_id: %s' % order_id)
    log.info(u'request.body = %s' % request.body)

    nid = cache.get('wechatpay_nid_' + hashlib.sha1(notify_id).hexdigest())
    if nid:
        log.info(u'duplicated notify, drop it')
        return HttpResponse('fail')
    body_dict = _wechatpay_xml_to_dict(request.body)
    data = {}
    for key, item in request.GET.items():
        data[key] = item
    for key, item in body_dict:
        data[key] = item
    data['trade_state'] = str(data['trade_state'])
    data['total_fee'] = data['total_fee'] + (data['discount'] or 0)
    log.debug(u'Charge.recharge data = %s' % data)
    if Charge.recharge(data, provider='wechatpay'):
        cache.set('wechatpay_nid_' + hashlib.sha1(notify_id).hexdigest(),
                  order_id, 90000)  # notify_id 保存25小时。
        log.info(u'wechatpay callback success')
        return HttpResponse('success')

    log.info(u'not a valid callback, ignore')
    return HttpResponse('fail')


def _wechatpay_xml_to_dict(content):
    """ 只能解析一层xml，如果多层的话，最好改成用库 xmltodict
    """
    xml_dict = {}
    doc = minidom.parseString(content)
    params = [ele for ele in doc.childNodes[0].childNodes
              if isinstance(ele, minidom.Element)]
    for param in params:
        if param.childNodes:
            text = param.childNodes[0]
            xml_dict[param.tagName] = text.data
        else:
            xml_dict[param.tagName] = ''

    return xml_dict


def para_filter(params):
    return {key: params[key]
            for key in params
            if key.lower() not in {'sign', 'sign_type'} and params[key]}


def _wechatpay_verify_notify(params):
    wechat_sign = params['sign']
    log.debug(u'wechat_sign = %s' % wechat_sign)
    filterParams = para_filter(params)
    filterParams['sign_type'] = 'MD5'
    string1 = convert_params_to_str_in_order(filterParams)
    stringSignTemp = string1 + '&key=%s' % settings.WECHATPAY_PARTNERKEY
    log.debug(u'stringSignTemp = %s' % stringSignTemp)
    md5 = hashlib.md5()
    md5.update(stringSignTemp)
    sign = md5.hexdigest().uppercase()
    log.debug(u'sign = %s' % sign)
    return wechat_sign == sign
