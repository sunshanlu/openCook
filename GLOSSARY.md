# openCook

openCook is a household recipe and meal-planning app. It turns cookbook photos into structured recipes, plans the week's meals, and keeps one shared shopping list that knows what is already at home.

## Language

**Recipe**:
A dish the household cooks, with the ingredients, amounts, steps and time it needs.
_Avoid_: Dish, meal, 食谱 (drifts toward "meal plan"; the Chinese rendering is 菜谱)

**Ingredient**:
One item a recipe calls for, with its amount and unit when the recipe states them.
_Avoid_: Item, component, 材料

**Pantry**:
The ingredients the household already has at home, checked before anything is bought.
_Avoid_: Stock, inventory, larder, 储藏室

**Staple**:
A background basic the meal planner ignores when judging what is missing — salt, oil, sugar, water. A staple is assumed always on hand, so it never counts as a missing ingredient and never drives variety.
_Avoid_: Staple food, 主食. Chinese 主食 means rice and noodles, i.e. staple *food*; this project means background basics the planner ignores, such as salt and oil. The Chinese rendering is **常备食材**.

**Cookbook**:
A named collection a household files its recipes under.
_Avoid_: Collection, folder, category

**Household**:
The group of people who share the same recipes, meal plan and shopping list.
_Avoid_: Family, account, group

**Meal plan**:
The rolling set of days for which the household has decided what to cook.
_Avoid_: Menu, schedule, calendar

**Meal type**:
Which eating occasion a recipe suits: breakfast, lunch, snack or dinner.
_Avoid_: Course, meal category

**Content language**:
The language the household reads its recipes in, governing photo extraction, category matching and shopping-list grouping. It is chosen separately from the interface language.
_Avoid_: Recipe language (the shipped UI label for it, too narrow — the concept covers scanning, categories and shopping-list grouping, not just recipe text), UI language

**Shopping list**:
The week's items to buy, built from the meal plan and the pantry and shared across the household.
_Avoid_: Grocery list, shopping cart

## 小红书导入

**点点**:
小红书网页版里的 AI 助手。导入流程把笔记链接交给它,读回结构化回答,不调用其他模型。
_Avoid_: AI、模型、聊天机器人

**博主清单**:
用户挑选的一组博主主页,是导入的唯一入口;不遍历搜索结果。
_Avoid_: 账号列表(会与登录账号混淆)

**采集**:
把博主主页下的全部视频笔记登记为待处理笔记。
_Avoid_: 爬取、抓取

**分类**:
判断一条视频笔记是不是菜谱。
_Avoid_: 筛选、过滤

**提取**:
把已判定为菜谱的视频笔记,转换成可导入 openCook 的结构化 Recipe。
_Avoid_: 解析、识别

**访问账号**:
执行分类或提取时登录小红书的账号。分类和提取各用一个不同的访问账号。
_Avoid_: 账号(与 Household 混淆)、号

**发送额度**:
一个访问账号在一个时间窗口内允许发起的点点请求数。重试和格式修正的每次发送都消耗额度。
_Avoid_: 配额、试验、批(窗口之外的"批"另指会话容量)

**访问额度**:
一个访问账号在一个时间窗口内允许打开的笔记页和点点页数量。与发送额度各自独立计数,任一用尽即停止该类动作。
_Avoid_: 频率限制

**活跃时段**:
一个访问账号每天允许持续访问小红书的总时长。时段之外账号完全静默。
_Avoid_: 工作时间

**爬坡**:
新策略启动初期,额度只取目标值的一半;连续数天未发生限流冻结才升到全额。
_Avoid_: 预热

**限流冻结**:
小红书返回安全限制页(访问频繁,代号 300013)后,该访问账号停止一切访问。冻结时长随连续冻结次数翻倍,到期后小量试探恢复;连续成功后重置。
_Avoid_: 封号(冻结是暂时的,封号是永久的)

**预筛**:
分类之前,用笔记标题、描述、标签等已采集信息剔除高置信度的非菜谱。宁可错放,不可错杀:拿不准的一律交给点点分类。
_Avoid_: 过滤

**增量采集**:
对已完整采集过的博主,只登记新发布的笔记。定期仍做一次完整对账。
_Avoid_: 重新爬取

**必需字段**:
提取结果必须包含的 Recipe 字段:名称、Ingredient、步骤。其余字段缺失时仍接受该结果。
_Avoid_: 关键字段

**页面状态未确认**:
无法可靠确认小红书页面当前是否允许继续处理笔记的状态。它不等同于访问账号未登录、需要验证码或处于限流冻结。
_Avoid_: 登录失效、账号异常（尚无明确证据时）
