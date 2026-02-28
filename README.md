# OrcaMet Client Portal

Automated risk management forecasts for rope access operations. Multi-model ensemble weather intelligence, delivered twice daily via a client portal.

## Architecture

- **Framework:** Django 5.1
- **Database:** PostgreSQL (Render)
- **Authentication:** Auth0 (Authlib)
- **Static files:** WhiteNoise
- **Hosting:** Render (auto-deploy from GitHub)
- **Forecast engine:** Multi-model ensemble (UKV, ECMWF, ICON-EU, ARPEGE)

## Project Structure

```
orcamet-portal/
├── manage.py
├── requirements.txt
├── build.sh                  # Render build script
├── render.yaml               # Render blueprint (one-click deploy)
├── .env.example              # Environment variable template
│
├── orcamet_portal/           # Django project config
│   ├── settings.py
│   ├── urls.py
│   ├── wsgi.py / asgi.py
│   ├── templates/            # Base HTML templates
│   └── static/               # CSS, images
│
├── accounts/                 # Auth0 login, user roles
│   ├── models.py             # Custom User (superadmin/client_admin/client_user)
│   ├── views.py              # Auth0 login/callback/logout
│   └── templates/
│
├── sites/                    # Clients, sites, thresholds
│   ├── models.py             # Client, Site, ThresholdProfile, ChangeLog
│   ├── admin.py              # Django admin for site management
│   └── templates/
│
├── forecasts/                # Forecast storage and generation
│   ├── models.py             # ForecastRun, HourlyForecast, UKRiskMap
│   ├── engine/               # Your Python forecast scripts (Phase 2)
│   └── management/commands/  # Django commands for cron jobs
│
└── dashboard/                # Client-facing portal views
    ├── views.py              # Dashboard home, site detail
    └── templates/
```

## User Roles

| Role | Access |
|------|--------|
| `superadmin` | Full access. Manages all clients, sites, users, thresholds. |
| `client_admin` | Sees own client's sites. Can edit thresholds. |
| `client_user` | Read-only access to own client's sites and forecasts. |

---

## Setup Guide

### Step 1: Create an Auth0 Account

1. Go to **https://auth0.com/signup**
2. Sign up with your email (free tier is fine — supports 25,000 users)
3. When asked for a tenant name, use something like `orcamet` — your domain will be `orcamet.uk.auth0.com`
4. Select **I'm building for my own company**

### Step 2: Create an Auth0 Application

1. In the Auth0 dashboard, go to **Applications → Applications**
2. Click **+ Create Application**
3. Name it **OrcaMet Portal**
4. Select **Regular Web Application**
5. Click **Create**
6. Go to the **Settings** tab and note down:
   - **Domain** (e.g. `orcamet.uk.auth0.com`)
   - **Client ID**
   - **Client Secret**
7. Scroll down to **Allowed Callback URLs** and add:
   ```
   http://localhost:8000/callback/, https://orcamet-portal.onrender.com/callback/
   ```
8. Scroll to **Allowed Logout URLs** and add:
   ```
   http://localhost:8000/, https://orcamet-portal.onrender.com/
   ```
9. Click **Save Changes**

### Step 3: Create a Render Account

1. Go to **https://dashboard.render.com/register**
2. Sign up (connecting your GitHub account is easiest)

### Step 4: Push Code to GitHub

1. Create a new repository on GitHub called `orcamet-portal`
2. From your local machine:
   ```bash
   cd orcamet-portal
   git init
   git add .
   git commit -m "Phase 1: Django project with Auth0 and Render config"
   git remote add origin https://github.com/YOUR_USERNAME/orcamet-portal.git
   git push -u origin main
   ```

### Step 5: Deploy to Render

**Option A: One-click Blueprint (recommended)**

1. In Render Dashboard, go to **Blueprints → New Blueprint Instance**
2. Connect your `orcamet-portal` GitHub repo
3. Render reads the `render.yaml` and creates your web service + database
4. Wait for the build to finish

**Option B: Manual Setup**

1. Create a **PostgreSQL** database (free tier)
   - Note the **Internal Database URL**
2. Create a **Web Service** pointing to your GitHub repo
   - **Build Command:** `./build.sh`
   - **Start Command:** `python -m gunicorn orcamet_portal.asgi:application -k uvicorn.workers.UvicornWorker`
3. Add environment variables (see Step 6)

### Step 6: Configure Environment Variables in Render

In your Render web service, go to **Environment → Add Environment Variable**:

| Key | Value |
|-----|-------|
| `DATABASE_URL` | *(auto-set if using Blueprint)* |
| `SECRET_KEY` | *(auto-generated if using Blueprint, or click Generate)* |
| `AUTH0_DOMAIN` | `orcamet.uk.auth0.com` *(your Auth0 domain)* |
| `AUTH0_CLIENT_ID` | *(from Auth0 dashboard)* |
| `AUTH0_CLIENT_SECRET` | *(from Auth0 dashboard)* |
| `OPENMETEO_API_KEY` | `7LSFsCE3nPhiRiBU` |
| `WEB_CONCURRENCY` | `4` |

### Step 7: Create Your Superadmin Account

1. In Render Dashboard, go to your web service → **Shell**
2. Run:
   ```bash
   python manage.py createsuperuser
   ```
3. Enter your username, email (use the same email you'll log in with via Auth0), and password
4. Then set yourself as superadmin:
   ```bash
   python manage.py shell
   ```
   ```python
   from accounts.models import User
   u = User.objects.get(username="steve")
   u.role = "superadmin"
   u.save()
   ```

### Step 8: Test It

1. Visit your Render URL (e.g. `https://orcamet-portal.onrender.com`)
2. Click **Log In** — you'll be redirected to Auth0
3. Create an Auth0 account with the same email as your superadmin
4. After login, you should see the Dashboard
5. Visit `/admin/` to access Django's admin panel

---

## Local Development

```bash
# Clone the repo
git clone https://github.com/YOUR_USERNAME/orcamet-portal.git
cd orcamet-portal

# Create virtual environment
python -m venv venv
source venv/bin/activate       # Mac/Linux
venv\Scripts\activate          # Windows

# Install dependencies
pip install -r requirements.txt

# Copy and configure environment
cp .env.example .env
# Edit .env with your Auth0 credentials

# Create local PostgreSQL database
# (or use SQLite by changing DATABASE_URL in .env to sqlite:///db.sqlite3)

# Run migrations
python manage.py migrate

# Create superadmin
python manage.py createsuperuser

# Run dev server
python manage.py runserver
```

Visit `http://localhost:8000`

---

## Phase Roadmap

- [x] **Phase 1:** Django skeleton + Auth0 + Render deployment + database schema
- [ ] **Phase 2:** Admin panel for adding sites (postcode geocoding), threshold management
- [ ] **Phase 3:** Forecast engine integration (your Python scripts as Django commands)
- [ ] **Phase 4:** Client portal views (site forecast heatmaps, text reports)
- [ ] **Phase 5:** UK risk map generation and display
- [ ] **Phase 6:** Cron jobs (Render Workflows) for twice-daily forecast updates
- [ ] **Phase 7:** ServiceM8 integration for automatic job discovery
