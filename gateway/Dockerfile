# Passerelle : configuration et portail copiés dans l'image (pas de montage depuis
# le poste : un dossier synchronisé, iCloud par exemple, provoque des erreurs d'E/S).
FROM nginx:1.27-alpine
COPY gateway/nginx.conf /etc/nginx/conf.d/default.conf
COPY portal /usr/share/nginx/html
RUN nginx -t
