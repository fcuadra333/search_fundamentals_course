"""Model layer.

Every call to a language model in this system goes through `LLMGateway`. No
module outside this package imports a provider SDK. We are going to change
model and provider; the rest of the code must not find out (CLAUDE.md).
"""
