package com.zawwar.zhub

open class ZhubException(message: String, cause: Throwable? = null) : RuntimeException(message, cause)
class AuthException(message: String) : ZhubException(message)
class ZhubConnectionException(message: String, cause: Throwable? = null) : ZhubException(message, cause)
class ManifestException(message: String) : ZhubException(message)
class CapabilityException(message: String) : ZhubException(message)
class HubException(message: String) : ZhubException(message)
