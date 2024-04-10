#!/bin/bash
set -x

function main {

  init_params "$@"
  run_benchmark

}

# init params
function init_params {
  for var in "$@"
  do
    case $var in
      --input_model=*)
          input_model=$(echo $var |cut -f2 -d=)
      ;;
      --batch_size=*)
          batch_size=$(echo $var |cut -f2 -d=)
      ;;
      --tokenizer=*)
          tokenizer=$(echo $var |cut -f2 -d=)
      ;;
      --mode=*)
          mode=$(echo $var |cut -f2 -d=)
      ;;
      --backend=*)
          backend=$(echo $var |cut -f2 -d=)
      ;;
      --seqlen=*)
          seqlen=$(echo $var |cut -f2 -d=)
      ;;
      --max_new_tokens=*)
          max_new_tokens=$(echo $var |cut -f2 -d=)
      ;;
      --iter_num=*)
          iter_num=$(echo $var |cut -f2 -d=)
      ;;
      --warmup_num=*)
          warmup_num=$(echo $var |cut -f2 -d=)
      ;;
      --intra_op_num_threads=*)
          intra_op_num_threads=$(echo $var |cut -f2 -d=)
      ;;
    esac
  done

}

# run_benchmark
function run_benchmark {
    
    # Check if the input_model ends with the filename extension ".onnx"
    if [[ $input_model =~ \.onnx$ ]]; then
        # If the string ends with the filename extension, get the path of the file
        input_model=$(dirname "$input_model")
    fi

    python main.py \
            --model_path ${input_model} \
            --batch_size=${batch_size-1} \
            --tokenizer=${tokenizer-meta-llama/Llama-2-7b-hf} \
            --tasks=${tasks-lambada_openai} \
            --mode=${mode} \
            --backend=${backend} \
            --seqlen=${seqlen-1024} \
            --max_new_tokens=${max_new_tokens-32} \
            --iter_num=${iter_num-10} \
            --warmup_num=${warmup_num-3} \
            --intra_op_num_threads=${intra_op_num_threads-24} \
            --benchmark
            
}

main "$@"

