# linkding-cn-adapters

[linkding-cn](https://github.com/WooHooDai/linkding-cn) 官方维护的所有站点适配器订阅源仓库。

## 使用方式

- 以管理员身份，进入管理后台的`网站适配`页面（设置 - 管理后台 - Site Adapters；网址示例: [http://localhost:9090/admin/site-adapters](http://localhost:9090/admin/site-adapters)）
- `网站适配管理` - `订阅` - `新增`
- 在`来源`处粘贴订阅源链接即可，如: [https://raw.githubusercontent.com/WooHooDai/linkding-cn-adapters/refs/heads/main/src/standard/adapters.jsonc](https://raw.githubusercontent.com/WooHooDai/linkding-cn-adapters/refs/heads/main/src/standard/adapters.jsonc)

## 订阅源

- [standard](https://raw.githubusercontent.com/WooHooDai/linkding-cn-adapters/refs/heads/main/src/standard/adapters.jsonc): 适配常见且无需登录凭据的网站

## 说明

- 新增请求、失效反馈、适配建议: 请在本仓库[**提 issue**](https://github.com/WooHooDai/linkding-cn-adapters/issues/new)，需附上 url、问题/需求描述、截图
    - 不是所有网站都需要编写适配规则，大部分结构简单/标准的网站 linkding-cn 内置引擎即可自动适配
    - 检索已适配域名及其规则: 每个订阅源都由一个配置文件（`adapters.jsonc`）和多个脚本文件组成，可在 adapters.jsonc 中查找
- 编写适配器请参考: [Site Adapters 指南](https://github.com/WooHooDai/linkding-cn/blob/main/docs/site-adapters.md)