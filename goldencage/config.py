# encoding=utf-8


APPWALLLOG_MAPPING = {
    'waps': {'identity': ('adv_id', 'key'),
             'cost': 'points',
             'user_id': 'key',
             'product_id': 'adv_id',
             'product_name': 'ad_name'
             },
    'youmi_ios': {'identity': 'order',
                  'cost': 'points',
                  'user_id': 'user',
                  'product_id': 'adid',
                  'product_name': 'ad'
                  },
    'youmi_adr': {'identity': 'order',
                  'cost': 'points',
                  'user_id': 'user',
                  'product_id': 'ad',
                  'product_name': 'ad',
                  }
}

PAYMENT_MAPPING = {
    'alipay': {'account': 'buyer_id',
               'email': 'buyer_email',
               'value': 'total_fee',
               'transaction_id': 'trade_no',
               'order_id': 'out_trade_no',
               'status': 'trade_status'
               }
}


EXCHANGE_RATE = 25  # 一RMB对应金币数
