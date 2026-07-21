try:
    from .rules import ComboRule, CustomerRule, DefaultChickenRule, FutureRule
except ImportError:
    from rules import ComboRule, CustomerRule, DefaultChickenRule, FutureRule


class BusinessRules:
    rules = [
        DefaultChickenRule(),
        ComboRule(),
        CustomerRule(),
        FutureRule(),
    ]

    @classmethod
    def apply(cls, text, parser=None):
        menu = text.upper()

        for rule in cls.rules:
            menu = rule.apply(menu, parser=parser)

        return menu

