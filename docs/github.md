# GitHub Publishing

## Local Repository

Initialize locally:

```powershell
git init
git add .
git status
git commit -m "Initial project inventory and documentation"
```

## Remote Repository

Create a private GitHub repository, then add it as a remote:

```powershell
git remote add origin https://github.com/<owner>/<repo>.git
git branch -M main
git push -u origin main
```

For command-line Git over HTTPS, GitHub requires a Personal Access Token instead of an account password. GitHub's official docs say password-based authentication for Git was removed in favor of more secure methods.

Recommended options:

- Git Credential Manager with browser login;
- SSH key;
- fine-grained Personal Access Token with access only to this repository.

## Pre-Push Checklist

Run:

```powershell
git status --short
git ls-files
rg -n "BEGIN OPENSSH|PRIVATE KEY|pass=|Authorization|VPNTYPE_AUTH|SUB_URL|Pro~|p5883022|brS-" .
```

Review any matches before pushing.
