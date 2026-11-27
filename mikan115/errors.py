class AppError(Exception):
    def __init__(self, code, message, status=400, *, uncertain=False):
        super().__init__(message)
        self.code, self.message, self.status, self.uncertain = code, message, status, uncertain


class ProviderError(AppError):
    def __init__(self, code='PROVIDER_UNAVAILABLE', message='上游服务暂时不可用，请稍后重试', *, uncertain=False):
        super().__init__(code, message, 502, uncertain=uncertain)
