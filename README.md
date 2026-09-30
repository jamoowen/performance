## interesting things i want to test

The first runnable HTTP baseline lives in [benchmarks/http](benchmarks/http/README.md).

## 1. memory consumption, request latency across different languages with REALISTIC tests
### why? -> I want to see which backend language is the most performant given my usual narrowly scoped needs
 **using sqlite as db
 ** we should probably seed the db's with a few thousand records?
 ** we should run the tests across a few different operations
 ** we should keep hitting the api's with requests until they begin to fallover
 ** we should test common things like json serialization
 ** im not sure if the db will be bottleneck first? if it is perhaps we need to provision postgres or something?
 ** ?? more things im not thinking of?
    a) golang using net/http and native go sql driver
        i) net/http
        ii) echo
        iii) gin
        iii) huma
        iv) fiber
    b) nodejs
        i) express
        ii) koa
        iii) nestJs
        iii) hono
    c) bun
        i) express
        ii) koa
        iii) nestJs
        iii) hono
    c) rust
        i) express
        ii) koa
        iii) nestJs
        iii) hono

## 2. Database comparison
### why? -> I want to see how well something like sqlite scales? at what point would i need something more powerful?
 ** not exactly sure how to test this?
 ** reads
 ** writes
 ** reads/writes with row locking?
    a) sqlite
    b) postgres
    c) ??

## 3. agentic tool comparisons
### why? -> does the underlying language of the harness actually make a difference and speed up the model at all?;
** ask an agent a question or give it a task within a large codebase
    a) go
    b) node
    c) rust
    d) bash
