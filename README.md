# 📚 Mi Biblioteca · Blog de Lecturas

Blog personal de lecturas generado automáticamente desde Goodreads.

## 🚀 Cómo publicar en GitHub Pages (paso a paso)

### 1. Crear el repositorio en GitHub

1. Ve a [github.com](https://github.com) e inicia sesión (o crea cuenta gratis)
2. Haz clic en el **+** de arriba a la derecha → **"New repository"**
3. Ponle el nombre que quieras, por ejemplo: `mi-biblioteca`
4. Márcalo como **Public** (necesario para GitHub Pages gratis)
5. Haz clic en **"Create repository"**

### 2. Subir estos archivos

La forma más fácil es desde la web de GitHub:

1. En tu repositorio vacío, haz clic en **"uploading an existing file"**
2. Arrastra y suelta **todos estos archivos** manteniendo la estructura de carpetas:
   ```
   .github/
     workflows/
       update.yml
   scripts/
     build.py
   index.html          ← el que ya tienes generado
   README.md
   ```
3. Haz clic en **"Commit changes"**

> **Truco**: Si tienes Git instalado en tu ordenador, puedes hacer:
> ```bash
> git init
> git add .
> git commit -m "Primera versión"
> git remote add origin https://github.com/TU_USUARIO/mi-biblioteca.git
> git push -u origin main
> ```

### 3. Activar GitHub Pages

1. Ve a tu repositorio → pestaña **"Settings"**
2. En el menú izquierdo, haz clic en **"Pages"**
3. En "Source", selecciona **"Deploy from a branch"**
4. Elige la rama **main** y carpeta **/ (root)**
5. Haz clic en **"Save"**

En 1-2 minutos tu web estará en:
**`https://TU_USUARIO.github.io/mi-biblioteca`** ✓

### 4. Configurar actualización automática

El archivo `.github/workflows/update.yml` ya está configurado para:
- Ejecutarse **cada día a las 6:00h** (hora España)
- Leer tu RSS de Goodreads automáticamente
- Regenerar el `index.html` con tus libros nuevos
- Hacer commit y push solo si hay cambios

Para que funcione, necesitas que tu **perfil de Goodreads sea público**:
1. En Goodreads → **Settings** (arriba derecha)
2. → **Privacy**
3. → "Who can see my profile" → **Everyone**
4. → "Who can see my bookshelves" → **Everyone**

### 5. Forzar actualización manual

En cualquier momento puedes ir a tu repositorio → pestaña **"Actions"** → **"Actualizar biblioteca"** → **"Run workflow"** → **"Run workflow"** (botón verde).

---

## 🔄 Flujo automático

```
Terminas un libro en Goodreads
         ↓
GitHub Actions (cada noche a las 6AM)
         ↓
Lee tu RSS: goodreads.com/review/list_rss/7001188?shelf=read
         ↓
Regenera index.html con portadas reales y datos actualizados
         ↓
Publica automáticamente en GitHub Pages ✓
```

---

## 📁 Estructura del proyecto

```
mi-biblioteca/
├── index.html                  ← La web (se regenera automáticamente)
├── scripts/
│   └── build.py                ← Script que lee el RSS y genera el HTML
├── .github/
│   └── workflows/
│       └── update.yml          ← Cron job de GitHub Actions
└── README.md
```

---

## ⚙️ Ejecutar localmente

Si quieres regenerar la web en tu ordenador:

```bash
# Necesitas Python 3.8+
python scripts/build.py

# Luego abre index.html en el navegador
```

No necesitas instalar ninguna librería extra, solo Python estándar.

---

## 🆓 ¿Cuánto cuesta?

**Todo gratis:**
- GitHub: repositorio público gratuito
- GitHub Pages: hosting gratuito
- GitHub Actions: hasta 2.000 minutos/mes gratis (este workflow usa ~1 minuto al día)
- Goodreads RSS: gratuito y sin límites

---

## 💡 Personalización

Para cambiar el nombre del blog, abre `scripts/build.py` y edita las líneas del template HTML que contienen `Mi Biblioteca`.

Para cambiar el ID de usuario de Goodreads, edita la línea:
```python
GOODREADS_USER_ID = "7001188"
```
