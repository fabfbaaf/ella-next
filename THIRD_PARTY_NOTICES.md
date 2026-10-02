# 第三方许可与资源说明

本项目作者为桔梗。自有源码、文档和原创发布资源适用根目录的 [MIT 许可证](LICENSE)，允许商业使用、修改和再分发，须保留版权声明与许可证。第三方软件、SDK、模型、图像、字体、音频以及其他第三方内容保留各自的版权和许可，不因与项目一同使用而改为 MIT。本说明不转授项目作者不拥有的第三方权利。

## 1.0 公开源码范围

1.0 公开源码快照排除以下原有本机资源；本机开发目录可继续保留它们：

- `apps/desktop/public/models/live2d/Hiyori/`：Hiyori 模型、动作、物理数据、姿态和纹理。
- `apps/desktop/public/models/live2d/live2dcubismcore.min.js`：Live2D Cubism Core。
- `apps/desktop/public/isla-pet.png`：原静态立绘，来源和公开分发授权未记录。
- `apps/desktop/src-tauri/icons/` 内原有图标：来源和公开分发授权未记录；如公开版本加入独立制作的替代图标，其来源应另行记载。
- 第三方程序和游戏依赖的本机缓存、已安装插件备份、游戏配置、存档和个人数据。

资源使用条件和本机导入位置见 [第三方资源说明](docs/第三方资源.md)。公开快照的排除范围不等同于认定上述资源不能在满足其条款的作品中使用。

公开版本新增的 `ella-placeholder.svg` 为本项目独立绘制的占位角色；公开图标由 `scripts/generate-release-icons.py` 独立生成。这些原创替代资源与自有代码一起采用 MIT，不使用原立绘、样本模型或原图标制作。

## Live2D

Hiyori Momose 是 Live2D 的原创样本角色，其模型和纹理不是 MIT 资源。使用时须遵守 [Free Material License Agreement](https://www.live2d.com/eula/live2d-free-material-license-agreement_en.html) 和 [Terms of Use for Live2D Cubism Sample Data](https://www.live2d.com/eula/live2d-sample-model-terms_en.html)。后者把 Hiyori 列为原创角色，禁止改变该角色的设计，并规定作品中使用样本角色时的版权声明。原始素材的再分发和作品内分发应分别按协议判断。

Cubism Core 适用 [Live2D Proprietary Software License Agreement](https://www.live2d.com/eula/live2d-proprietary-software-license-agreement_en.html)，属于独立的专有组件。该协议对可再分发代码的原样分发、下游许可和保留声明等设有条件，不能将它纳入项目 MIT 许可。官方 [Cubism Web Samples 许可说明](https://raw.githubusercontent.com/Live2D/CubismWebSamples/develop/LICENSE.md) 分别说明 Core、SDK 组件和 Hiyori 等模型的许可。

本项目的 `pixi-live2d-display` 0.4.0 适用 MIT，Copyright (c) 2020 Guan，见 [原项目许可证](https://raw.githubusercontent.com/guansss/pixi-live2d-display/v0.4.0/LICENSE)。该插件的 MIT 许可不覆盖另行使用的 Core 或角色资源；分发包含该插件重要部分的构建产物时须保留其版权和许可。

## GABS 与游戏桥接

GABS 服务端和 Bannerlord.GABS 游戏模块是不同的第三方项目，不能用其中一个的声明代替另一个。

| 组件 | 来源及许可 | 分发处理 |
| --- | --- | --- |
| GABS 服务端 `gabs.exe` | [pardeike/GABS](https://github.com/pardeike/GABS)，[MIT](https://raw.githubusercontent.com/pardeike/GABS/main/LICENSE)，Copyright (c) 2024 GABS Contributors | 随 EXE 分发其版权和完整许可。许可证副本保存于 `scripts/licenses/GABS-MIT.txt`；实际发行时核对所用二进制版本。 |
| Bannerlord.GABS v1.0.0 | [BUTR/Bannerlord.GABS](https://github.com/BUTR/Bannerlord.GABS)，[MIT](https://raw.githubusercontent.com/BUTR/Bannerlord.GABS/v1.0.0/LICENSE)，Copyright (c) 2026 Vitaly Mikhailov | 游戏资源准备脚本保存独立模块许可证。 |
| SMAPI 4.5.2 | [Pathoschild/SMAPI](https://github.com/Pathoschild/SMAPI)，[LGPL-3.0](https://raw.githubusercontent.com/Pathoschild/SMAPI/4.5.2/LICENSE.txt) | 游戏资源准备脚本保存 LGPL、GPL、未修改版本的对应源码与安装说明；分发时仍须保留适用版权和第三方依赖通知。 |
| Fabric API 0.156.0+26.2 | [FabricMC/fabric-api](https://github.com/FabricMC/fabric-api)，[Apache-2.0](https://raw.githubusercontent.com/FabricMC/fabric-api/0.156.0%2B26.2/LICENSE) | 游戏资源准备脚本保存许可，并保留相关 JAR 内的 LICENSE/NOTICE。 |
| Fabric Loader 0.19.5 | [FabricMC/fabric-loader](https://github.com/FabricMC/fabric-loader)，[Apache-2.0](https://raw.githubusercontent.com/FabricMC/fabric-loader/0.19.5/LICENSE) | 游戏资源准备脚本保存许可，并保留相关 JAR 内的 LICENSE/NOTICE。 |

`scripts/prepare-game-setup.py` 还从固定官方版本获取 Bannerlord.BLSE、Harmony、ButterLib、UIExtenderEx 和 MBOptionScreen 的许可，并将其与生成的游戏资源一同保存。各版本、下载地址和校验值以脚本为准。该流程不会把游戏本体或存档的权利赋予本项目。

## 其他依赖与发行包

前端、Rust 和 Python 依赖由各自的依赖清单与锁文件声明，适用各上游项目的许可。公开源码不包含 `node_modules`、Python 虚拟环境、编译缓存或第三方二进制资源缓存。

若另行发布安装包，需要针对实际捆绑的依赖和运行时保存适用 LICENSE、NOTICE、版权和必要的对应源码。本文列出的组件并非整个安装包的完整传递依赖清单，不能以本文替代实际发行包的依赖清点。
