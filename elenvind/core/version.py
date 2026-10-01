major = 0
minor = 3
patch = 1
stage = "dev"

def get_version() -> str:
    return f"v{major}.{minor}.{patch}-{stage}"
