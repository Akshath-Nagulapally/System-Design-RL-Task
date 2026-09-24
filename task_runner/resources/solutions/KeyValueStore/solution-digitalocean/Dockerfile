FROM --platform=$BUILDPLATFORM golang:1.25.1 AS build
ARG TARGETOS TARGETARCH
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY cmd ./cmd
RUN CGO_ENABLED=0 GOOS=$TARGETOS GOARCH=$TARGETARCH go build -trimpath -ldflags='-s -w' -o /kv ./cmd/kv

FROM alpine:3.22
COPY --from=build /kv /kv
USER 10001:10001
EXPOSE 8080
ENTRYPOINT ["/kv"]
