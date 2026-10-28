# Station calibrations

One JSON file per station and market type (`KNYC_high.json`, `KNYC_low.json`, …), written by
`wxbot calibrate`. Each holds, per forecast model and lead time:

- **bias**: how far the model's daily extreme typically misses the official climate-report value (recency-weighted)
- **rmse**: the model's typical error after removing that bias
- **weights**: how much the blend trusts each model (inverse error, shrunk toward sensible priors)
- **sigma**: the error spread of the final blended forecast; this sets how wide the probability distribution is

Stations without a file use conservative defaults. Re-fit monthly. Biases drift with the seasons:

```bash
wxbot calibrate --kind both --days 365
```
