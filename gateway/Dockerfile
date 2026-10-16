# API Gateway : Kong en mode déclaratif (sans base de données). La configuration est
# copiée dans l'image et validée à la construction : une erreur dans kong.yml fait
# échouer « docker compose build » avec un message explicite.
FROM kong:3.9.1
ENV KONG_DATABASE=off \
    KONG_DECLARATIVE_CONFIG=/kong/kong.yml \
    KONG_UNTRUSTED_LUA_SANDBOX_REQUIRES=resty.http
COPY gateway/kong.yml /kong/kong.yml
RUN kong config parse /kong/kong.yml
