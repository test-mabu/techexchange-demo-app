FROM maven:3.8.4-eclipse-temurin-11 AS build
WORKDIR /app
COPY .m2 /root/.m2
COPY pom.xml .
COPY src ./src
# -o  = offline (use only the pre-populated .m2 — no internet access)
# -Dmaven.repo.local ensures the COPY'd repo is used, not the image default ~/.m2
RUN mvn -o -B -Dmaven.repo.local=/root/.m2 package -DskipTests

FROM eclipse-temurin:11.0.17_8-jre
LABEL vulnerability="CVE-2021-44228"
LABEL log4j-version="2.14.1"

# Alternative newer runtime base image for air-gapped use:
# FROM eclipse-temurin:11.0.29_7-jre
# LABEL vulnerability=""
# LABEL log4j-version="2.25.2"

RUN groupadd -r appuser && useradd -r -g appuser appuser

WORKDIR /app
COPY --from=build /app/target/vulnerable-echo-service-*.jar app.jar
# Static curl binary placed by load.sh — no shared libraries, no apt-get needed
COPY curl /usr/local/bin/curl
RUN chown -R appuser:appuser /app

USER appuser

ENV JAVA_OPTS="-Xmx512m -Xms256m"
ENV SERVER_PORT=8085
ENV LOG4J2_DISABLE_JNDI=false

EXPOSE 8085

HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:${SERVER_PORT}/health || exit 1

CMD java $JAVA_OPTS -Dserver.port=$SERVER_PORT -jar app.jar
