# ---------- 1단계: 빌드 ----------
FROM eclipse-temurin:21-jdk AS build
WORKDIR /workspace

# 의존성만 먼저 받아 캐시한다. (소스만 바뀌면 이 레이어는 재사용됨)
COPY gradlew settings.gradle build.gradle ./
COPY gradle ./gradle
RUN sed -i 's/\r$//' gradlew && chmod +x gradlew \
    && ./gradlew dependencies --no-daemon -q > /dev/null

COPY src ./src
RUN ./gradlew bootJar --no-daemon -x test

# ---------- 2단계: 실행 ----------
FROM eclipse-temurin:21-jre
WORKDIR /app
RUN useradd --system --uid 10001 traceon
COPY --from=build /workspace/build/libs/*.jar app.jar
USER traceon
EXPOSE 8080
ENTRYPOINT ["java", "-jar", "/app/app.jar"]
