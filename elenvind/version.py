major = 0
minor = 1
patch = 0
stage = "dev"

def get_version() -> str:
    return f"v{major}.{minor}.{patch}-{stage}"
