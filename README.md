# 中国电商免登录搜索 MCP

一个纯 HTTP、只读、无需登录的 MCP 服务，支持：

- 拼多多（pdd）
- 京东（jd）
- 淘宝（taobao）

服务通过公开网页索引发现商品、价格与一般优惠，不启动浏览器，不读取 Cookie，
也不处理登录、验证码、加购或下单。Windows 和纯终端 Linux 均可运行。

## 工具

- `search_products`：同时或单独搜索三个平台，返回公开索引价格、一般优惠、链接和时间信息。
- `build_search_links`：生成三个平台的官方搜索链接，不访问网络。
- `parse_product_link`：识别三个平台的商品链接并提取商品 ID，不访问网络。

## 搜索返回字段

每个平台都返回 `official_search_url` 和单独的 `direct_product_links`。每条价格/优惠
结果还会尽量返回：

- `displayed_price_cny`：公开索引中观察到的价格。
- `price_kind`：到手价、活动价或普通展示价等类型。
- `general_promotions`：满减、优惠券、折扣、直降、百亿补贴、包邮等一般优惠。
- `product_url`：公开索引能确认的官方商品直链。
- `source_url`：产生价格或优惠信息的公开来源链接。
- `best_available_link`：商品直链优先，否则使用促销来源链接。
- `indexed_date` / `freshness`：索引日期和新鲜度提示。
- `price_confidence`：明确标记为索引价格，而不是结算价。

拼多多商品直链通常不会被公开搜索引擎稳定索引，因此拼多多结果可能只有公开促销
来源链接和官方搜索入口。工具不会伪造商品直链。

## 价格边界

此工具不登录，所以不能获取：

- 账号专享券、会员折扣或新人价；
- 地区、收货地址或支付方式相关优惠；
- App 独占活动；
- 订单确认页最终实付价。

公开索引也可能过期或把多个 SKU 混在一起。下单前必须在平台商品页确认准确 SKU，
并以订单确认页价格为准。

## 运行

启动 MCP 服务：

~~~powershell
python server.py
~~~

安装最小依赖：

~~~powershell
python -m pip install -r requirements.txt
~~~

运行单元测试：

~~~powershell
python -m unittest discover -s tests -v
~~~

执行真实免登录搜索：

~~~powershell
python server.py --live-test "150元以内 游戏 静音鼠标"
~~~

Linux 示例：

~~~bash
python3 -m pip install -r requirements.txt
python3 server.py --live-test "150元以内 游戏 静音鼠标"
~~~

## Codex 注册

将路径替换为本机仓库中的 `server.py`：

~~~powershell
codex mcp add cn-ecommerce -- python C:\path\to\cn-ecommerce-mcp\server.py
~~~

Linux：

~~~bash
codex mcp add cn-ecommerce -- python3 /path/to/cn-ecommerce-mcp/server.py
~~~

修改后需开启新聊天或重启 Codex，让现有 MCP 进程重新加载代码。
