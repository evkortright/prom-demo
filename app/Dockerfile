# Stage 1 — Builder
FROM golang:1.22-alpine AS builder

WORKDIR /src

# Copy dependency files and download them.
# go.sum will be generated inside the container.
COPY go.mod ./
RUN go mod download || true

# Copy the source and tidy/build.
COPY main.go .
RUN go mod tidy && CGO_ENABLED=0 GOOS=linux go build -o /app .

# Stage 2 — Runtime
FROM alpine:3.19

RUN apk --no-cache add ca-certificates

WORKDIR /app

COPY --from=builder /app ./prom-demo

EXPOSE 8080

CMD ["./prom-demo"]
