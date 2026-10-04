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
