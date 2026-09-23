FROM golang:1.23-alpine AS build
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 go build -o /out/kvserver ./cmd/kvserver && CGO_ENABLED=0 go build -o /out/kvimport ./cmd/kvimport

FROM alpine:3.20
RUN adduser -D -H -u 10001 kv
COPY --from=build /out/kvserver /usr/local/bin/kvserver
COPY --from=build /out/kvimport /usr/local/bin/kvimport
USER 10001:10001
EXPOSE 8080
ENTRYPOINT ["/usr/local/bin/kvserver"]
